"""The Anthropic adapter (spec 19.15), on the official `anthropic` SDK.

Rules this follows, from the current API:
  * no `temperature`, `top_p` or `top_k` (current models reject them); depth of thinking is
    `output_config.effort`, taken from the route; `thinking` is left out because these models think
    adaptively on their own
  * the server-side refusal fallback is on (`fallbacks: "default"` with its beta flag), so a model
    that declines a request is retried on a fallback inside the same call
  * `stop_reason` is checked before any content is used: `refusal` and `max_tokens` become errors
  * the SDK's own retries are off (`max_retries=0`); `RetryingLLMProvider` retries, and only before
    the first token
Errors are typed and never carry prompt text or credentials in their message.
"""

from collections.abc import AsyncIterator
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from covenantiq.ai.llm.base import (
    LLMBadRequest,
    LLMRefused,
    LLMRequest,
    LLMStreamEvent,
    LLMTruncated,
    LLMUnavailable,
    LLMUsage,
    StructuredResult,
    T,
)
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.core.errors import CovenantError

FALLBACK_BETA = "server-side-fallback-2026-07-01"

_UNAVAILABLE = (
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,
    anthropic.GatewayTimeoutError,
)


def map_error(exc: Exception) -> CovenantError:
    """An SDK exception as one of ours: retryable (`LLMUnavailable`) or not (`LLMBadRequest`)."""
    if isinstance(exc, _UNAVAILABLE):
        return LLMUnavailable(f"the model provider is unavailable ({type(exc).__name__})")
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        if status >= 500 or status in (408, 409, 429, 529):
            return LLMUnavailable(f"the model provider is unavailable (HTTP {status})")
        return LLMBadRequest(f"the model provider rejected the request (HTTP {status})")
    return LLMBadRequest(f"the model provider call failed ({type(exc).__name__})")


def _messages(request: LLMRequest) -> list[Any]:
    return [{"role": m.role, "content": m.content} for m in request.messages]


def _usage(raw: Any) -> LLMUsage:
    return LLMUsage(
        input_tokens=int(getattr(raw, "input_tokens", 0) or 0),
        output_tokens=int(getattr(raw, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(raw, "cache_read_input_tokens", 0) or 0),
    )


def _check_stop(stop_reason: str | None, request: LLMRequest) -> None:
    if stop_reason == "refusal":
        raise LLMRefused("The model declined to answer this request.")
    if stop_reason == "max_tokens":
        raise LLMTruncated(
            f"The {request.route} answer was cut off at {request.max_tokens} tokens."
        )


class AnthropicProvider:
    def __init__(
        self,
        api_key: str,
        routes: RouteTable,
        *,
        client: AsyncAnthropic | None = None,
        fallbacks: bool = True,
    ) -> None:
        self._client = client or AsyncAnthropic(api_key=api_key, max_retries=0)
        self._routes = routes
        self._fallbacks = fallbacks
        self.model_name = routes.model_for("answer")

    def _params(self, request: LLMRequest) -> dict[str, Any]:
        route = self._routes.get(request.route)
        params: dict[str, Any] = {
            "model": self._routes.model_for(request.route),
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": _messages(request),
            "output_config": {"effort": route.effort},
            "timeout": request.timeout_s,
        }
        if self._fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        try:
            async with self._client.beta.messages.stream(**self._params(request)) as stream:
                async for text in stream.text_stream:
                    yield LLMStreamEvent("delta", text=text)
                final = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise map_error(exc) from None
        yield LLMStreamEvent("usage", usage=_usage(final.usage))
        _check_stop(final.stop_reason, request)
        yield LLMStreamEvent("stop", stop_reason=final.stop_reason)

    async def structured(self, request: LLMRequest, schema: type[T]) -> StructuredResult[T]:
        try:
            response = await self._client.beta.messages.parse(
                **self._params(request), output_format=schema
            )
        except anthropic.AnthropicError as exc:
            raise map_error(exc) from None
        except ValueError:
            # The SDK parses before we can look at the stop reason, so a structured answer that
            # was cut off at the token limit, or is not valid JSON, surfaces here.
            raise LLMRefused(
                f"The {request.route} answer could not be read as the expected structure "
                f"(it may have been cut off at {request.max_tokens} tokens)."
            ) from None
        _check_stop(response.stop_reason, request)
        parsed = response.parsed_output
        if parsed is None:
            raise LLMRefused("The model returned no usable structured answer.")
        return StructuredResult(
            value=parsed, usage=_usage(response.usage), model=str(response.model)
        )

    async def count_tokens(self, request: LLMRequest) -> int:
        try:
            counted = await self._client.messages.count_tokens(
                model=self._routes.model_for(request.route),
                system=request.system,
                messages=_messages(request),
            )
        except anthropic.AnthropicError as exc:
            raise map_error(exc) from None
        return int(counted.input_tokens)
