"""The OpenAI adapter, for OpenAI itself or any server speaking its Chat Completions format.

Written on `httpx` (as the OpenAI embedding provider is), so no SDK is needed. Rules it follows:
  * `max_completion_tokens`, never `max_tokens` (newer models reject the old name) and no
    `temperature`, `top_p` or other sampling parameter (reasoning models reject them)
  * `reasoning_effort` is sent from the route's effort when `send_effort` is on; turn it off
    (`CIQ_OPENAI_SEND_EFFORT=false`) for a model or server that has no such parameter
  * structured answers use `response_format: json_schema` without `strict`, and are validated by
    the pydantic schema here: strict mode forbids optional fields, which our schemas have
  * `finish_reason` is checked before any content is used: `length` and `content_filter`, and a
    `refusal` field, become errors
  * no automatic retries here; `RetryingLLMProvider` retries, and only before the first token
Errors are typed and never carry prompt text or credentials in their message.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import ValidationError

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

DEFAULT_BASE_URL = "https://api.openai.com/v1"
CONNECT_TIMEOUT_SECONDS = 10.0
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def map_status(status: int) -> CovenantError:
    """An HTTP error status as one of ours: retryable (`LLMUnavailable`) or not."""
    if status >= 500 or status in (408, 409, 429):
        return LLMUnavailable(f"the model provider is unavailable (HTTP {status})")
    if status in (401, 403):
        return LLMBadRequest(f"the model provider rejected the credentials (HTTP {status})")
    return LLMBadRequest(f"the model provider rejected the request (HTTP {status})")


def _transport_error(exc: httpx.HTTPError) -> CovenantError:
    return LLMUnavailable(f"the model provider is unreachable ({type(exc).__name__})")


def _usage(raw: Any) -> LLMUsage:
    raw = raw or {}
    details = raw.get("prompt_tokens_details") or {}
    return LLMUsage(
        input_tokens=int(raw.get("prompt_tokens", 0) or 0),
        output_tokens=int(raw.get("completion_tokens", 0) or 0),
        cache_read_tokens=int(details.get("cached_tokens", 0) or 0),
    )


def _check_finish(finish_reason: str | None, request: LLMRequest) -> None:
    if finish_reason == "content_filter":
        raise LLMRefused("The model declined to answer this request.")
    if finish_reason == "length":
        raise LLMTruncated(
            f"The {request.route} answer was cut off at {request.max_tokens} tokens."
        )


class OpenAIProvider:
    def __init__(
        self,
        api_key: str,
        routes: RouteTable,
        *,
        base_url: str = DEFAULT_BASE_URL,
        send_effort: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"}
        )
        self._routes = routes
        self._send_effort = send_effort
        self.model_name = routes.model_for("answer")

    def _body(self, request: LLMRequest) -> dict[str, Any]:
        route = self._routes.get(request.route)
        body: dict[str, Any] = {
            "model": self._routes.model_for(request.route),
            "max_completion_tokens": request.max_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                *({"role": m.role, "content": m.content} for m in request.messages),
            ],
        }
        if self._send_effort:
            body["reasoning_effort"] = _EFFORT[route.effort]
        return body

    @staticmethod
    def _timeout(request: LLMRequest) -> httpx.Timeout:
        return httpx.Timeout(request.timeout_s, connect=CONNECT_TIMEOUT_SECONDS)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        body = {**self._body(request), "stream": True, "stream_options": {"include_usage": True}}
        finish: str | None = None
        usage = LLMUsage()
        refusal = False
        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=body, timeout=self._timeout(request)
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise map_status(response.status_code)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    if chunk.get("usage"):
                        usage = _usage(chunk["usage"])
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("refusal"):
                            refusal = True
                        if delta.get("content"):
                            yield LLMStreamEvent("delta", text=delta["content"])
                        finish = choice.get("finish_reason") or finish
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from None
        except json.JSONDecodeError:
            raise LLMUnavailable("the model provider sent an unreadable stream") from None
        yield LLMStreamEvent("usage", usage=usage)
        if refusal:
            raise LLMRefused("The model declined to answer this request.")
        _check_finish(finish, request)
        yield LLMStreamEvent("stop", stop_reason=finish)

    async def structured(self, request: LLMRequest, schema: type[T]) -> StructuredResult[T]:
        body = {
            **self._body(request),
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": False,
                },
            },
        }
        try:
            response = await self._client.post(
                "/chat/completions", json=body, timeout=self._timeout(request)
            )
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from None
        if response.status_code >= 400:
            raise map_status(response.status_code)
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMUnavailable("the model provider sent an unreadable answer") from None
        if message.get("refusal"):
            raise LLMRefused("The model declined to answer this request.")
        _check_finish(choice.get("finish_reason"), request)
        try:
            value = schema.model_validate_json(message.get("content") or "")
        except ValidationError:
            raise LLMRefused(
                f"The {request.route} answer could not be read as the expected structure."
            ) from None
        return StructuredResult(
            value=value, usage=_usage(data.get("usage")), model=str(data.get("model", ""))
        )

    async def count_tokens(self, request: LLMRequest) -> int:
        """An estimate (about four characters a token): the API has no token-counting call."""
        chars = len(request.system) + sum(len(m.content) for m in request.messages)
        return chars // 4 + 1
