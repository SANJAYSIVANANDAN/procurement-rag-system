import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

from covenantiq.ai.embeddings.base import EmbeddingProvider, EmbeddingUnavailable

T = TypeVar("T")


class RetryingEmbeddingProvider:
    """Retries transient provider failures with exponential backoff and jitter.

    Only `EmbeddingUnavailable` is retried. A rejected input or a bad key fails at once: the
    same request would fail the same way.
    """

    def __init__(
        self,
        inner: EmbeddingProvider,
        *,
        attempts: int = 4,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._attempts = attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        self.model = inner.model
        self.dim = inner.dim

    async def _retry(self, call: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(1, self._attempts + 1):
            try:
                return await call()
            except EmbeddingUnavailable:
                if attempt == self._attempts:
                    raise
                delay = min(self._max_delay, self._base_delay * 2 ** (attempt - 1))
                await self._sleep(delay * (0.5 + random.random() / 2))  # noqa: S311
        raise AssertionError("unreachable")  # pragma: no cover

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._retry(lambda: self._inner.embed_documents(texts))

    async def embed_query(self, text: str) -> list[float]:
        return await self._retry(lambda: self._inner.embed_query(text))

    async def aclose(self) -> None:
        await self._inner.aclose()
