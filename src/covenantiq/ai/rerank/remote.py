import time
from collections.abc import Callable

import httpx

from covenantiq.ai.rerank.base import RerankRejected, RerankUnavailable, squash
from covenantiq.ai.rerank.breaker import FAILURES_BEFORE_OPENING, OPEN_FOR_SECONDS, CircuitBreaker

__all__ = ["FAILURES_BEFORE_OPENING", "MAX_DOCUMENT_CHARS", "OPEN_FOR_SECONDS", "RemoteReranker"]

# Longer passages are cut before they are sent; a chunk is about 350 tokens (about 1,400
# characters) and the model reads at most 512 tokens anyway.
MAX_DOCUMENT_CHARS = 4000


class RemoteReranker:
    """Talks to the reranker process over HTTP, so the model, PyTorch and their threads never run
    inside the API or a worker.

    A reranker that is down or slow must not slow every request down with it: after three
    failures in a row the client fails immediately for 30 seconds, and the caller keeps the fused
    order (spec 19.8).
    """

    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        timeout_seconds: float = 3.0,
        max_documents: int = 100,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self._max_documents = max_documents
        # Told to the server so it stops scoring when this client has given up (minus a margin
        # for the network).
        self._deadline_ms = max(1, int(timeout_seconds * 1000 * 0.9))
        self._breaker = CircuitBreaker(clock=clock)
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 1.0)),
        )

    async def rerank(self, query: str, docs: list[str], top_k: int) -> list[tuple[int, float]]:
        return [(index, squash(raw)) for index, raw in await self.rerank_raw(query, docs, top_k)]

    async def rerank_raw(self, query: str, docs: list[str], top_k: int) -> list[tuple[int, float]]:
        """Like `rerank` but with the model's own unbounded scores (for calibration)."""
        if not docs or top_k <= 0:
            return []
        if len(docs) > self._max_documents:
            raise RerankRejected(
                f"{len(docs)} documents is more than the {self._max_documents} allowed"
            )
        if self._breaker.is_open():
            raise RerankUnavailable("the reranker is being skipped after repeated failures")

        payload = {
            "query": query,
            "documents": [doc[:MAX_DOCUMENT_CHARS] for doc in docs],
            "top_k": min(top_k, len(docs)),
            "deadline_ms": self._deadline_ms,
        }
        try:
            response = await self._client.post("/v1/rerank", json=payload)
        except httpx.HTTPError as exc:
            self._breaker.record_failure()
            raise RerankUnavailable(f"the reranker did not answer: {type(exc).__name__}") from exc

        status = response.status_code
        if status == 503 or status >= 500:
            self._breaker.record_failure()
            raise RerankUnavailable(f"the reranker returned HTTP {status}")
        if status >= 400:
            raise RerankRejected(f"the reranker rejected the request (HTTP {status})")
        try:
            results = [
                (int(row["index"]), float(row["score"])) for row in response.json()["results"]
            ]
        except (KeyError, TypeError, ValueError) as exc:
            self._breaker.record_failure()
            raise RerankUnavailable("the reranker returned an unreadable body") from exc
        if any(not 0 <= index < len(docs) for index, _ in results):
            self._breaker.record_failure()
            raise RerankUnavailable("the reranker returned an index outside the request")

        self._breaker.record_success()
        return results

    async def aclose(self) -> None:
        await self._client.aclose()
