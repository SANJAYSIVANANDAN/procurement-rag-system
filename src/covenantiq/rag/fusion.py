"""Reciprocal rank fusion (spec 19.6).

    rrf(d)   = sum over lists  w_list / (k + rank_list(d))    k = 60
    fused(d) = rrf(d) * boost                          boost = 1.1 for a wanted clause type

Rank-based on purpose: cosine similarity and full-text rank live on different scales, so a weighted
sum of the scores would be unstable, while ranks are comparable across queries.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class FusedCandidate:
    chunk_id: UUID
    rrf_score: float  # before the clause-type boost
    fused_score: float
    dense_rank: int | None
    lexical_rank: int | None


def _ranks(ordered: Sequence[UUID]) -> dict[UUID, int]:
    ranks: dict[UUID, int] = {}
    for position, chunk_id in enumerate(ordered, start=1):
        ranks.setdefault(chunk_id, position)  # a repeated id keeps its best rank
    return ranks


def rrf_fuse(
    dense: Sequence[UUID],
    lexical: Sequence[UUID],
    *,
    k: int = 60,
    weight_dense: float = 1.0,
    weight_lexical: float = 1.0,
    clause_types: Mapping[UUID, Sequence[str]] | None = None,
    boost_types: Collection[str] = (),
    boost: float = 1.1,
    limit: int = 30,
) -> list[FusedCandidate]:
    """Merge two best-first id lists into one, best first, at most `limit` long.

    Ties are broken by the better of the two ranks and then by id, so the result is deterministic.
    """
    dense_ranks, lexical_ranks = _ranks(dense), _ranks(lexical)
    wanted = set(boost_types)
    fused: list[FusedCandidate] = []
    for chunk_id in dict.fromkeys([*dense, *lexical]):
        dense_rank, lexical_rank = dense_ranks.get(chunk_id), lexical_ranks.get(chunk_id)
        score = 0.0
        if dense_rank is not None:
            score += weight_dense / (k + dense_rank)
        if lexical_rank is not None:
            score += weight_lexical / (k + lexical_rank)
        matches = bool(
            wanted and clause_types and wanted.intersection(clause_types.get(chunk_id, ()))
        )
        fused.append(
            FusedCandidate(
                chunk_id=chunk_id,
                rrf_score=score,
                fused_score=score * (boost if matches else 1.0),
                dense_rank=dense_rank,
                lexical_rank=lexical_rank,
            )
        )
    fused.sort(
        key=lambda c: (
            -c.fused_score,
            min(r for r in (c.dense_rank, c.lexical_rank) if r is not None),
            str(c.chunk_id),
        )
    )
    return fused[:limit]
