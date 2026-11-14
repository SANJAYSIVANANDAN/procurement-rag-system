from collections.abc import Sequence
from typing import Any

from voyageai import error as voyage_error
from voyageai.client_async import AsyncClient

from covenantiq.ai.embeddings.base import (
    EmbeddingAuthFailed,
    EmbeddingRejected,
    EmbeddingUnavailable,
)

REQUEST_TIMEOUT_SECONDS = 60.0


class VoyageEmbeddingProvider:
    """Voyage AI embeddings. `voyage-law-2` returns 1024 dimensions and has no other size."""

    def __init__(self, api_key: str, model: str, dim: int) -> None:
        self.model = model
        self.dim = dim
        # Retries are done by `RetryingEmbeddingProvider`, so the client makes one attempt.
        self._client = AsyncClient(api_key=api_key, max_retries=0, timeout=REQUEST_TIMEOUT_SECONDS)

    async def _embed(self, texts: Sequence[str], input_type: str) -> list[list[float]]:
        try:
            result = await self._client.embed(list(texts), model=self.model, input_type=input_type)
        except voyage_error.AuthenticationError as exc:
            raise EmbeddingAuthFailed("the embedding provider rejected the API key") from exc
        except (voyage_error.InvalidRequestError, voyage_error.MalformedRequestError) as exc:
            raise EmbeddingRejected(f"the embedding provider rejected the input: {exc}") from exc
        except (
            voyage_error.RateLimitError,
            voyage_error.ServerError,
            voyage_error.ServiceUnavailableError,
            voyage_error.APIConnectionError,
            voyage_error.APIError,
            TimeoutError,
        ) as exc:
            raise EmbeddingUnavailable(f"the embedding provider is unavailable: {exc}") from exc
        embeddings: list[list[float]] = [list(map(float, row)) for row in result.embeddings]
        if len(embeddings) != len(texts) or any(len(row) != self.dim for row in embeddings):
            raise EmbeddingRejected(
                f"the provider returned an unexpected shape for {self.model}: "
                f"expected {len(texts)} x {self.dim}"
            )
        return embeddings

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts, "document")

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "query"))[0]

    async def aclose(self) -> None:
        close: Any = getattr(self._client, "aclose", None)
        if close is not None:
            await close()
