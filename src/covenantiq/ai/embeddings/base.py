"""The embedding provider contract (spec 15.2)."""

from collections.abc import Sequence
from typing import Protocol

from covenantiq.core.errors import CovenantError, TransientError


class EmbeddingUnavailable(TransientError):
    """The provider could not be reached or throttled the request: retry later."""


class EmbeddingRejected(CovenantError):
    """The provider refused this input (a 4xx other than auth or throttling).

    Retrying the same batch will not help; the caller splits it to find the offending text.
    """


class EmbeddingAuthFailed(CovenantError):
    """The API key was rejected. Retrying cannot fix it, so the job fails with a clear code."""


class EmbeddingProvider(Protocol):
    model: str
    dim: int

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Vectors for passages to be stored, in the order given."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """A vector for a search query. Providers that distinguish query from document input
        (Voyage) use the matching mode, which matters for retrieval quality."""
        ...

    async def aclose(self) -> None: ...
