"""The reranker contract (spec 19.8 and 23)."""

import math
from typing import Protocol

from covenantiq.core.errors import CovenantError, TransientError


class RerankUnavailable(TransientError):
    """The reranker could not answer in time (down, busy, timed out). The caller keeps the
    fused order and records that reranking was skipped."""


class RerankRejected(CovenantError):
    """The reranker refused the request (for example too many documents). Not worth retrying."""


class Reranker(Protocol):
    model: str

    async def rerank(self, query: str, docs: list[str], top_k: int) -> list[tuple[int, float]]:
        """The `top_k` best documents as `(index into docs, score)`, best first. Scores are in
        (0, 1) so that thresholds mean the same whichever reranker is configured."""
        ...

    async def aclose(self) -> None: ...


def squash(raw_score: float) -> float:
    """Map a raw, unbounded relevance logit into (0, 1) with a sigmoid. The mapping keeps the
    order, so it never changes a ranking; it only gives scores a common scale."""
    if raw_score >= 0:
        return 1.0 / (1.0 + math.exp(-raw_score))
    exponential = math.exp(raw_score)
    return exponential / (1.0 + exponential)
