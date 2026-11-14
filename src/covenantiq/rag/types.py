"""The data that flows through retrieval (spec 19.1)."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from covenantiq.db.scope import OwnershipScope


@dataclass(frozen=True)
class RetrievalRequest:
    scope: OwnershipScope
    query: str  # a standalone natural-language query
    document_ids: Sequence[UUID]  # already resolved: owned, ready, not deleted
    keywords: Sequence[str] = ()  # full-text terms; empty means "extract them from the query"
    phrases: Sequence[str] = ()  # quoted phrases kept intact in the full-text query
    clause_type_boost: Sequence[str] = ()
    k_dense: int | None = None  # None: take it from config/rag.yaml
    k_lexical: int | None = None
    k_final: int | None = None
    rerank: bool = True
    expand_parents: bool = True


@dataclass
class RetrievedChunk:
    chunk_id: UUID
    clause_id: UUID
    document_id: UUID
    document_title: str
    ordinal: int
    text: str
    page_start: int | None
    page_end: int | None
    clause_ref: str | None
    heading_path: list[str]
    clause_types: list[str]
    injection_score: float
    dense_rank: int | None = None  # 1-based position in the vector results
    lexical_rank: int | None = None
    rrf_score: float = 0.0
    rerank_score: float | None = None


@dataclass
class ParentClause:
    clause_id: UUID
    document_id: UUID
    document_title: str
    clause_ref: str | None
    heading: str | None
    heading_path: list[str]
    text: str
    token_count: int
    page_start: int | None
    page_end: int | None


@dataclass
class RetrievalTrace:
    """What happened, for tuning and for the logs. Ids, ranks, scores and timings: never text."""

    query_sha: str
    keywords: list[str]
    fts_text: str
    embedding_model: str
    document_count: int
    exact_scan: bool = False
    query_embedding_cached: bool = False
    dense: list[tuple[UUID, int, float]] = field(default_factory=list)  # id, rank, similarity
    lexical: list[tuple[UUID, int, float]] = field(default_factory=list)  # id, rank, ts_rank_cd
    fused: list[tuple[UUID, float]] = field(default_factory=list)  # id, fused score
    reranked: list[tuple[UUID, float]] = field(default_factory=list)  # id, rerank score
    rerank_skipped: bool = False
    rerank_skipped_reason: str | None = None
    rerank_cache_hits: int = 0
    notes: list[str] = field(default_factory=list)  # degradations, in words
    timings_ms: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_sha": self.query_sha,
            "keywords": self.keywords,
            "fts_text": self.fts_text,
            "embedding_model": self.embedding_model,
            "document_count": self.document_count,
            "exact_scan": self.exact_scan,
            "query_embedding_cached": self.query_embedding_cached,
            "dense": [[str(i), r, round(s, 4)] for i, r, s in self.dense],
            "lexical": [[str(i), r, round(s, 4)] for i, r, s in self.lexical],
            "fused": [[str(i), round(s, 5)] for i, s in self.fused],
            "reranked": [{"chunk_id": str(i), "score": round(s, 4)} for i, s in self.reranked],
            "rerank_skipped": self.rerank_skipped,
            "rerank_skipped_reason": self.rerank_skipped_reason,
            "rerank_cache_hits": self.rerank_cache_hits,
            "notes": self.notes,
            "timings_ms": self.timings_ms,
        }


@dataclass
class RetrievalResult:
    chunks: list[RetrievedChunk]  # final: reranked (or fused, if reranking was skipped)
    parents: dict[UUID, ParentClause]  # clause_id -> the whole clause
    trace: RetrievalTrace
    # chunk_id -> the chunks next to it, for a clause too long to include whole
    neighbours: dict[UUID, list[RetrievedChunk]] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextSource:
    """One numbered source `[C1]` in the prompt, and what it stands for."""

    marker: str  # "C1"
    chunk_ids: tuple[UUID, ...]
    clause_id: UUID
    document_id: UUID
    document_title: str
    clause_ref: str | None
    page_start: int | None
    page_end: int | None
    text: str  # exactly the text shown to the model
    injection_flagged: bool = False


@dataclass
class ContextBundle:
    text: str  # the <source> blocks, ready for the prompt
    sources: list[ContextSource]
    tokens: int
    dropped_groups: int = 0  # groups that did not fit the budget

    @property
    def marker_map(self) -> dict[str, ContextSource]:
        return {source.marker: source for source in self.sources}


@dataclass
class EvidenceVerdict:
    sufficient: bool
    level: str  # strong | moderate | weak | none
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
