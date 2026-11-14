"""The retrieval engine: the one place that searches contracts (spec 19.1, 19.7).

    query -> keywords -> embed -> dense top 30 + lexical top 30 (concurrently)
          -> fuse with RRF -> top 30 -> rerank (3 s, optional) -> top 8 -> parent clauses

The chat graph, contract analysis and vendor assessment all call `retrieve`, with different
parameters. Reranking is optional at every level: switched off, absent, too slow or failing, the
fused order is kept and `trace.rerank_skipped` says so (the evidence gate is stricter then).
"""

import asyncio
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from covenantiq.ai.embeddings.base import EmbeddingProvider, EmbeddingUnavailable
from covenantiq.ai.rerank.base import Reranker, RerankRejected, RerankUnavailable
from covenantiq.core.logging import get_logger
from covenantiq.db.repositories.chunk_search import ChunkRecord, ChunkSearchRepository, SearchHit
from covenantiq.ingestion.chunking.tokens import estimate_tokens
from covenantiq.rag.cache import (
    Cache,
    NullCache,
    decode_vector,
    embedding_key,
    encode_vector,
    query_sha,
    rerank_key,
)
from covenantiq.rag.config import RagConfig
from covenantiq.rag.fusion import FusedCandidate, rrf_fuse
from covenantiq.rag.query_analysis import build_fts_text, extract_keywords
from covenantiq.rag.types import (
    ParentClause,
    RetrievalRequest,
    RetrievalResult,
    RetrievalTrace,
    RetrievedChunk,
)

log = get_logger(__name__)

# How the engine reaches the database: a fresh read-only session per call, so the two searches
# can run at the same time (one session cannot run two queries at once).
SearchSessionFactory = Callable[[], AbstractAsyncContextManager[ChunkSearchRepository]]


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


class RetrievalEngine:
    def __init__(
        self,
        search_session: SearchSessionFactory,
        embedder: EmbeddingProvider,
        reranker: Reranker | None,
        config: RagConfig,
        *,
        cache: Cache | None = None,
        env: str = "dev",
        exact_scan_threshold: int = 5000,
    ) -> None:
        self._session = search_session
        self._embedder = embedder
        self._reranker = reranker
        self._config = config
        self._cache: Cache = cache or NullCache()
        self._env = env
        self._exact_threshold = exact_scan_threshold

    async def aclose(self) -> None:
        """Close the embedding provider and the reranker the engine was built with."""
        await self._embedder.aclose()
        if self._reranker is not None:
            await self._reranker.aclose()

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        started = time.monotonic()
        query = " ".join(request.query.split())
        if not query:
            raise ValueError("the query must not be empty")
        config = self._config.retrieval
        keywords = list(request.keywords)
        phrases = list(request.phrases)
        if not keywords and not phrases:
            found = extract_keywords(query, max_keywords=config.max_keywords)
            keywords, phrases = found.keywords, found.phrases
        fts_text = build_fts_text(keywords, phrases)
        trace = RetrievalTrace(
            query_sha=query_sha(query),
            keywords=keywords,
            fts_text=fts_text,
            embedding_model=self._embedder.model,
            document_count=len(request.document_ids),
        )
        if not request.document_ids:
            trace.timings_ms["total"] = _ms(started)
            return RetrievalResult(chunks=[], parents={}, trace=trace)

        k_dense = request.k_dense or config.k_dense
        k_lexical = request.k_lexical or config.k_lexical
        k_final = request.k_final or config.k_final

        mark = time.monotonic()
        vector = await self._query_vector(query, trace)
        trace.timings_ms["embed"] = _ms(mark)

        mark = time.monotonic()
        dense_hits, lexical_hits = await self._search(
            request, vector, fts_text, k_dense, k_lexical, trace
        )
        trace.timings_ms["search"] = _ms(mark)
        trace.dense = [(h.chunk_id, rank, h.score) for rank, h in enumerate(dense_hits, start=1)]
        trace.lexical = [
            (h.chunk_id, rank, h.score) for rank, h in enumerate(lexical_hits, start=1)
        ]

        mark = time.monotonic()
        records = await self._hydrate(request, [h.chunk_id for h in (*dense_hits, *lexical_hits)])
        fused = rrf_fuse(
            [h.chunk_id for h in dense_hits],
            [h.chunk_id for h in lexical_hits],
            k=config.rrf_k,
            weight_dense=config.weight_dense,
            weight_lexical=config.weight_lexical,
            clause_types={chunk_id: r.clause_types for chunk_id, r in records.items()},
            boost_types=request.clause_type_boost,
            boost=config.clause_type_boost,
            limit=config.rerank_candidates,
        )
        candidates = [self._chunk(records[c.chunk_id], c) for c in fused if c.chunk_id in records]
        trace.fused = [(f.chunk_id, f.fused_score) for f in fused if f.chunk_id in records]
        trace.timings_ms["fuse"] = _ms(mark)

        mark = time.monotonic()
        final = await self._rerank(request, query, candidates, k_final, trace)
        trace.timings_ms["rerank"] = _ms(mark)

        mark = time.monotonic()
        parents, neighbours = (
            await self._expand(request, final) if request.expand_parents else ({}, {})
        )
        trace.timings_ms["expand"] = _ms(mark)
        trace.timings_ms["total"] = _ms(started)
        log.info(
            "rag.retrieved",
            chunks=len(final),
            rerank_skipped=trace.rerank_skipped,
            reason=trace.rerank_skipped_reason,
            **{f"{name}_ms": value for name, value in trace.timings_ms.items()},
        )
        return RetrievalResult(chunks=final, parents=parents, trace=trace, neighbours=neighbours)

    # ---- query embedding --------------------------------------------------------------------

    async def _query_vector(self, query: str, trace: RetrievalTrace) -> list[float] | None:
        key = embedding_key(self._env, self._embedder.model, query)
        cached = (await self._cache.get_many([key]))[0]
        if cached is not None:
            trace.query_embedding_cached = True
            return decode_vector(cached)
        try:
            vector = await self._embedder.embed_query(query)
        except EmbeddingUnavailable:
            # no vector means no dense search, but keyword search can still answer
            trace.notes.append("dense search skipped: the embedding provider is unavailable")
            log.warning("rag.embedding_unavailable")
            return None
        await self._cache.set_many(
            {key: encode_vector(vector)}, self._config.retrieval.query_embedding_cache_seconds
        )
        return vector

    # ---- the two searches -------------------------------------------------------------------

    async def _search(
        self,
        request: RetrievalRequest,
        vector: list[float] | None,
        fts_text: str,
        k_dense: int,
        k_lexical: int,
        trace: RetrievalTrace,
    ) -> tuple[list[SearchHit], list[SearchHit]]:
        documents = list(request.document_ids)
        model = self._embedder.model

        async def dense() -> list[SearchHit]:
            if vector is None:
                return []
            async with self._session() as repo:
                count = await repo.chunk_count(request.scope, documents, model)
                trace.exact_scan = count < self._exact_threshold
                if count == 0:
                    # vectors from different models are not comparable, so none were searched
                    trace.notes.append(
                        f"dense search found nothing to search: no chunk is embedded with {model} "
                        "(after changing the embedding model, run `covenantiq reembed`)"
                    )
                return await repo.dense_search(
                    request.scope,
                    vector,
                    document_ids=documents,
                    embedding_model=model,
                    k=k_dense,
                    exact=trace.exact_scan,
                    ef_search=self._config.retrieval.ef_search,
                )

        async def lexical() -> list[SearchHit]:
            if not fts_text:
                return []
            async with self._session() as repo:
                return await repo.lexical_search(
                    request.scope, fts_text, document_ids=documents, k=k_lexical
                )

        dense_hits, lexical_hits = await asyncio.gather(dense(), lexical())
        return dense_hits, lexical_hits

    async def _hydrate(
        self, request: RetrievalRequest, chunk_ids: list[UUID]
    ) -> dict[UUID, ChunkRecord]:
        unique = list(dict.fromkeys(chunk_ids))
        if not unique:
            return {}
        async with self._session() as repo:
            records = await repo.get_chunks(request.scope, unique, list(request.document_ids))
        return {record.chunk_id: record for record in records}

    @staticmethod
    def _chunk(record: ChunkRecord, fused: FusedCandidate) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=record.chunk_id,
            clause_id=record.clause_id,
            document_id=record.document_id,
            document_title=record.document_title,
            ordinal=record.ordinal,
            text=record.text,
            page_start=record.page_start,
            page_end=record.page_end,
            clause_ref=record.clause_ref,
            heading_path=record.heading_path,
            clause_types=record.clause_types,
            injection_score=record.injection_score,
            dense_rank=fused.dense_rank,
            lexical_rank=fused.lexical_rank,
            rrf_score=fused.fused_score,
        )

    # ---- reranking --------------------------------------------------------------------------

    async def _rerank(
        self,
        request: RetrievalRequest,
        query: str,
        candidates: list[RetrievedChunk],
        k_final: int,
        trace: RetrievalTrace,
    ) -> list[RetrievedChunk]:
        """Rerank the fused candidates, or keep their order. Never raises: a reranker that is
        off, absent, slow or broken costs only the reranking."""
        if not candidates:
            return []

        def keep_fused(reason: str) -> list[RetrievedChunk]:
            trace.rerank_skipped = True
            trace.rerank_skipped_reason = reason
            return candidates[:k_final]

        if not request.rerank:
            return keep_fused("switched off for this request")
        if self._reranker is None:
            return keep_fused("no reranker is configured")

        reranker = self._reranker
        sha = trace.query_sha
        config = self._config.retrieval
        keys = {
            c.chunk_id: rerank_key(self._env, reranker.model, sha, c.chunk_id) for c in candidates
        }
        scores: dict[UUID, float] = {}
        for chunk_id, cached in zip(
            keys, await self._cache.get_many(list(keys.values())), strict=True
        ):
            if cached is not None:
                scores[chunk_id] = float(cached.decode())
        trace.rerank_cache_hits = len(scores)

        missing = [c for c in candidates if c.chunk_id not in scores]
        if missing:
            documents = [self._rerank_text(c) for c in missing]
            try:
                ranked = await asyncio.wait_for(
                    reranker.rerank(query, documents, len(missing)),
                    timeout=config.rerank_timeout_seconds,
                )
            except TimeoutError:
                return keep_fused(f"timed out after {config.rerank_timeout_seconds:g} s")
            except (RerankUnavailable, RerankRejected) as exc:
                return keep_fused(f"{type(exc).__name__}: {exc.message}")
            fresh: dict[UUID, float] = {missing[index].chunk_id: score for index, score in ranked}
            scores.update(fresh)
            await self._cache.set_many(
                {keys[chunk_id]: repr(score).encode() for chunk_id, score in fresh.items()},
                config.rerank_cache_seconds,
            )
            if len(scores) < len(candidates):
                return keep_fused("the reranker did not score every candidate")

        for chunk in candidates:
            chunk.rerank_score = scores[chunk.chunk_id]
        ordered = sorted(
            candidates, key=lambda c: -(c.rerank_score or 0.0)
        )  # stable: ties keep fused order
        trace.reranked = [(c.chunk_id, c.rerank_score or 0.0) for c in ordered]
        return ordered[:k_final]

    def _rerank_text(self, chunk: RetrievedChunk) -> str:
        """`{heading path} | {text}`, cut to the configured length (spec 19.8)."""
        path = " > ".join(chunk.heading_path)
        text = f"{path} | {chunk.text}" if path else chunk.text
        return text[: self._config.retrieval.rerank_doc_max_chars]

    # ---- parent clauses ---------------------------------------------------------------------

    async def _expand(
        self, request: RetrievalRequest, chunks: list[RetrievedChunk]
    ) -> tuple[dict[UUID, ParentClause], dict[UUID, list[RetrievedChunk]]]:
        if not chunks:
            return {}, {}
        documents = list(request.document_ids)
        parent_max = self._config.context.parent_max_tokens
        async with self._session() as repo:
            clauses = await repo.get_clauses(
                request.scope, list({c.clause_id for c in chunks}), documents
            )
            parents = {
                clause.clause_id: ParentClause(
                    clause_id=clause.clause_id,
                    document_id=clause.document_id,
                    document_title=clause.document_title,
                    clause_ref=clause.clause_ref,
                    heading=clause.heading,
                    heading_path=clause.heading_path,
                    text=clause.text,
                    token_count=estimate_tokens(clause.text),
                    page_start=clause.page_start,
                    page_end=clause.page_end,
                )
                for clause in clauses
            }
            # a clause too long to include whole is shown as the hit plus the chunks beside it
            long_hits = [
                c
                for c in chunks
                if c.clause_id in parents and parents[c.clause_id].token_count > parent_max
            ]
            positions = {
                (c.document_id, c.ordinal + step)
                for c in long_hits
                for step in (-1, 1)
                if c.ordinal + step >= 0
            }
            adjacent = await repo.get_adjacent_chunks(request.scope, positions, documents)
        by_position = {(r.document_id, r.ordinal): r for r in adjacent}
        neighbours: dict[UUID, list[RetrievedChunk]] = {}
        for hit in long_hits:
            beside = [
                by_position[(hit.document_id, hit.ordinal + step)]
                for step in (-1, 1)
                if (hit.document_id, hit.ordinal + step) in by_position
                and by_position[(hit.document_id, hit.ordinal + step)].clause_id == hit.clause_id
            ]
            neighbours[hit.chunk_id] = [
                RetrievedChunk(
                    chunk_id=r.chunk_id,
                    clause_id=r.clause_id,
                    document_id=r.document_id,
                    document_title=r.document_title,
                    ordinal=r.ordinal,
                    text=r.text,
                    page_start=r.page_start,
                    page_end=r.page_end,
                    clause_ref=r.clause_ref,
                    heading_path=r.heading_path,
                    clause_types=r.clause_types,
                    injection_score=r.injection_score,
                )
                for r in beside
            ]
        return parents, neighbours
