import time
from collections.abc import Callable

import httpx

from covenantiq.ai.rerank.base import RerankRejected, RerankUnavailable
from covenantiq.ai.rerank.breaker import CircuitBreaker

BASE_URL = "https://api.cohere.com"
# A chunk is about 350 tokens; this leaves room for a long table row and keeps the billed size down.
MAX_TOKENS_PER_DOC = 1024


class CohereReranker:
    """Cohere's hosted reranker (`POST /v2/rerank`).

    Contract text is sent to Cohere's API; say so in the deployment notes. Cohere's scores are
    already normalised to [0, 1], so they are returned as they are. Any failure (timeout, rate
    limit, outage, a rejected key) raises `RerankUnavailable` and the caller keeps the fused order;
    after three in a row the breaker skips the call for 30 seconds so a dead API costs nothing.
    Nothing identifying the project is sent: no client-name header, no document ids.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        timeout_seconds: float = 3.0,
        max_documents: int = 100,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self._max_documents = max_documents
        self._breaker = CircuitBreaker(clock=clock, name="rerank.cohere")
        self._client = client or httpx.AsyncClient(
            base_url=(base_url or BASE_URL).rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 1.0)),
        )

    async def rerank(self, query: str, docs: list[str], top_k: int) -> list[tuple[int, float]]:
        if not docs or top_k <= 0:
            return []
        if len(docs) > self._max_documents:
            raise RerankRejected(
                f"{len(docs)} documents is more than the {self._max_documents} allowed"
            )
        if self._breaker.is_open():
            raise RerankUnavailable("the reranker is being skipped after repeated failures")

        payload = {
            "model": self.model,
            "query": query,
            "documents": docs,
            "top_n": min(top_k, len(docs)),
            "max_tokens_per_doc": MAX_TOKENS_PER_DOC,
        }
        try:
            response = await self._client.post("/v2/rerank", json=payload)
        except httpx.HTTPError as exc:
            self._breaker.record_failure()
            raise RerankUnavailable(f"Cohere did not answer: {type(exc).__name__}") from exc

        status = response.status_code
        if status in (401, 403):
            # not worth retrying, but counted so a bad key does not make every query call out
            self._breaker.record_failure()
            raise RerankUnavailable("Cohere rejected the API key")
        if status == 429 or status >= 500:
            self._breaker.record_failure()
            raise RerankUnavailable(f"Cohere returned HTTP {status}")
        if status >= 400:
            raise RerankRejected(f"Cohere rejected the request (HTTP {status})")

        try:
            results = [
                (int(row["index"]), float(row["relevance_score"]))
                for row in response.json()["results"]
            ]
        except (KeyError, TypeError, ValueError) as exc:
            self._breaker.record_failure()
            raise RerankUnavailable("Cohere returned an unreadable body") from exc
        if any(not 0 <= index < len(docs) for index, _ in results):
            self._breaker.record_failure()
            raise RerankUnavailable("Cohere returned an index outside the request")

        self._breaker.record_success()
        results.sort(key=lambda pair: (-pair[1], pair[0]))
        return results

    async def aclose(self) -> None:
        await self._client.aclose()
