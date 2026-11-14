from collections.abc import Sequence

import httpx

from covenantiq.ai.embeddings.base import (
    EmbeddingAuthFailed,
    EmbeddingRejected,
    EmbeddingUnavailable,
)

REQUEST_TIMEOUT_SECONDS = 60.0


class OpenAICompatEmbeddingProvider:
    """Any server exposing `POST {base_url}/embeddings` in the OpenAI format (vLLM, Ollama,
    LM Studio, a gateway). Used when Voyage is not an option, for example in an air-gapped
    deployment. The model's dimension must equal the `document_chunks.embedding` column width."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dim: int,
        *,
        request_dimensions: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.dim = dim
        # OpenAI's text-embedding-3 models return a shorter vector when asked with `dimensions`;
        # other servers may reject the field, so it is only sent when requested.
        self._request_dimensions = request_dimensions
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    async def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            payload: dict[str, object] = {"model": self.model, "input": list(texts)}
            if self._request_dimensions:
                payload["dimensions"] = self.dim
            response = await self._client.post("/embeddings", json=payload)
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(f"the embedding endpoint is unreachable: {exc}") from exc
        status = response.status_code
        if status in (401, 403):
            raise EmbeddingAuthFailed("the embedding endpoint rejected the API key")
        if status == 429 or status >= 500:
            raise EmbeddingUnavailable(f"the embedding endpoint returned HTTP {status}")
        if status >= 400:
            raise EmbeddingRejected(f"the embedding endpoint rejected the input (HTTP {status})")
        try:
            rows = sorted(response.json()["data"], key=lambda row: row["index"])
            embeddings = [[float(x) for x in row["embedding"]] for row in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingRejected("the embedding endpoint returned an unreadable body") from exc
        if len(embeddings) != len(texts) or any(len(row) != self.dim for row in embeddings):
            raise EmbeddingRejected(
                f"the endpoint returned an unexpected shape: expected {len(texts)} x {self.dim}"
            )
        return embeddings

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text]))[0]

    async def aclose(self) -> None:
        await self._client.aclose()
