"""Retrieval metrics (spec 28.2, 28.3): did the passage that answers the question come back?

`retrieved` is the final list of passage texts, best first. `relevant` is the labelled passages.
An example with no relevant passage (an unanswerable question) has nothing to find and contributes
nothing.
"""

from collections.abc import Sequence

from eval.metrics.stats import Ratio
from eval.metrics.text import OVERLAP_THRESHOLD, overlaps, similarity

SIMILARITY_THRESHOLD = 0.5


def _first_relevant_rank(retrieved: Sequence[str], relevant: Sequence[str]) -> int | None:
    for rank, text in enumerate(retrieved, start=1):
        if any(overlaps(text, passage, OVERLAP_THRESHOLD) for passage in relevant):
            return rank
    return None


def hit_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> Ratio:
    """1 if a relevant passage is among the first `k` results."""
    if not relevant:
        return 0.0, 0.0
    rank = _first_relevant_rank(retrieved, relevant)
    return (1.0 if rank is not None and rank <= k else 0.0), 1.0


def reciprocal_rank(retrieved: Sequence[str], relevant: Sequence[str]) -> Ratio:
    """1 / the rank of the first relevant passage, 0 if none came back. Their mean is the MRR."""
    if not relevant:
        return 0.0, 0.0
    rank = _first_relevant_rank(retrieved, relevant)
    return (0.0 if rank is None else 1.0 / rank), 1.0


def context_precision(
    retrieved: Sequence[str], references: Sequence[str], threshold: float = SIMILARITY_THRESHOLD
) -> Ratio:
    """Non-LLM context precision: the share of retrieved passages matching a reference passage."""
    if not references or not retrieved:
        return 0.0, 0.0  # nothing to find, or nothing came back: precision is undefined
    matched = sum(
        1 for text in retrieved if max(similarity(text, ref) for ref in references) >= threshold
    )
    return float(matched), float(len(retrieved))


def context_recall(
    retrieved: Sequence[str], references: Sequence[str], threshold: float = SIMILARITY_THRESHOLD
) -> Ratio:
    """Non-LLM context recall: the share of reference passages some retrieved passage matches."""
    if not references:
        return 0.0, 0.0
    matched = sum(
        1
        for ref in references
        if retrieved and max(similarity(text, ref) for text in retrieved) >= threshold
    )
    return float(matched), float(len(references))
