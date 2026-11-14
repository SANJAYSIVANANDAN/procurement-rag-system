import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable

from covenantiq.ai.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMStreamEvent,
    LLMUnavailable,
    StructuredResult,
    T,
)
from covenantiq.core.logging import get_logger

log = get_logger(__name__)


class RetryingLLMProvider:
    """Retries transient failures, twice, with backoff (spec 22.4).

    A streamed answer is retried only if it failed before its first event. Once a token has reached
    the user, a retry would repeat visible text, so the failure propagates and the message ends
    `failed` with whatever was streamed. Only `LLMUnavailable` is retried: a refusal, a truncated
    answer or a rejected request would fail the same way again.
    """

    def __init__(
        self,
        inner: LLMProvider,
        *,
        retries: int = 2,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._retries = retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        self.model_name = inner.model_name

    async def _pause(self, attempt: int) -> None:
        delay = min(self._max_delay, self._base_delay * 2**attempt)
        await self._sleep(delay * (0.5 + random.random() / 2))  # noqa: S311

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        for attempt in range(self._retries + 1):
            started = False
            try:
                async for event in self._inner.stream(request):
                    started = True
                    yield event
                return
            except LLMUnavailable:
                if started or attempt == self._retries:
                    raise
                log.warning("llm.retry", route=request.route, attempt=attempt + 1)
                await self._pause(attempt)

    async def structured(self, request: LLMRequest, schema: type[T]) -> StructuredResult[T]:
        for attempt in range(self._retries + 1):
            try:
                return await self._inner.structured(request, schema)
            except LLMUnavailable:
                if attempt == self._retries:
                    raise
                log.warning("llm.retry", route=request.route, attempt=attempt + 1)
                await self._pause(attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    async def count_tokens(self, request: LLMRequest) -> int:
        return await self._inner.count_tokens(request)
