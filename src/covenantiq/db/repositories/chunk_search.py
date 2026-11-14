"""Dense and lexical retrieval over `document_chunks` (spec 15.7, 19.4, 19.5).

Every query here filters on the owner, on the documents the caller may search, and on the document
itself being `ready` and not deleted. The last filter is deliberate redundancy: the caller is
supposed to pass only ready, owned documents, but a deleted document must never surface even if a
stale id slips through, and the cost of checking it in SQL is one join.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select, func, literal_column, select, text, tuple_

from covenantiq.core.enums import DocumentStatus
from covenantiq.db.models import Document, DocumentChunk, DocumentClause
from covenantiq.db.repositories.base import Repository
from covenantiq.db.scope import OwnershipScope


@dataclass(frozen=True)
class SearchHit:
    chunk_id: UUID
    score: float  # cosine similarity (dense) or ts_rank_cd (lexical); higher is better


@dataclass(frozen=True)
class ChunkRecord:
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
    has_table: bool


@dataclass(frozen=True)
class ClauseRecord:
    clause_id: UUID
    document_id: UUID
    document_title: str
    clause_ref: str | None
    heading: str | None
    heading_path: list[str]
    text: str
    page_start: int | None
    page_end: int | None


class ChunkSearchRepository(Repository):
    def _visible(
        self, scope: OwnershipScope, statement: Select[Any], documents: Collection[UUID]
    ) -> Select[Any]:
        """The filters every search shares. `statement` must already select from document_chunks
        joined to documents."""
        scoped = scope.apply(statement, DocumentChunk)
        return scoped.where(
            Document.owner_id == scope.owner_id,
            Document.deleted_at.is_(None),
            Document.status == DocumentStatus.READY,
            DocumentChunk.document_id.in_(list(documents)),
        )

    async def chunk_count(
        self, scope: OwnershipScope, document_ids: Collection[UUID], embedding_model: str
    ) -> int:
        """Embedded chunks in the documents: decides between an index scan and an exact scan."""
        if not document_ids:
            return 0
        counted = cast(
            "Select[Any]",
            select(func.count())
            .select_from(DocumentChunk)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.embedding_model == embedding_model,
                DocumentChunk.embedding.is_not(None),
            ),
        )
        return int(
            (await self.session.execute(self._visible(scope, counted, document_ids))).scalar_one()
        )

    async def _tune_vector_search(self, *, exact: bool, ef_search: int) -> None:
        # `SET LOCAL` lasts for this transaction only, so nothing leaks to other queries.
        await self.session.execute(text("SELECT '[1,2]'::vector"))  # loads pgvector's settings
        await self.session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
        iterative = (
            await self.session.execute(text("SELECT current_setting('hnsw.iterative_scan', true)"))
        ).scalar()
        if iterative is not None:  # pgvector 0.8 and later: keeps recall when filters are tight
            await self.session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        if exact:  # a small candidate set: scan everything, perfect recall, still fast
            await self.session.execute(text("SET LOCAL enable_indexscan = off"))

    async def dense_search(
        self,
        scope: OwnershipScope,
        vector: Sequence[float],
        *,
        document_ids: Collection[UUID],
        embedding_model: str,
        k: int,
        exact: bool = False,
        ef_search: int = 100,
    ) -> list[SearchHit]:
        """The `k` chunks nearest to `vector` by cosine distance, best first."""
        if not document_ids or k <= 0:
            return []
        await self._tune_vector_search(exact=exact, ef_search=ef_search)
        distance = DocumentChunk.embedding.cosine_distance(list(vector))
        statement = cast(
            "Select[Any]",
            select(DocumentChunk.id, (1 - distance).label("similarity"))
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.embedding_model == embedding_model,
                DocumentChunk.embedding.is_not(None),
            ),
        )
        rows = await self.session.execute(
            self._visible(scope, statement, document_ids)
            .order_by(distance, DocumentChunk.id)
            .limit(k)
        )
        return [SearchHit(row.id, float(row.similarity)) for row in rows]

    async def lexical_search(
        self,
        scope: OwnershipScope,
        fts_text: str,
        *,
        document_ids: Collection[UUID],
        k: int,
    ) -> list[SearchHit]:
        """Full-text search with `websearch_to_tsquery`, which never raises on odd input. The text
        is a bound parameter, never part of the SQL string."""
        if not document_ids or k <= 0 or not fts_text.strip():
            return []
        query = func.websearch_to_tsquery(literal_column("'english'::regconfig"), fts_text)
        rank = func.ts_rank_cd(DocumentChunk.tsv, query, 32)
        statement = cast(
            "Select[Any]",
            select(DocumentChunk.id, rank.label("rank"))
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.tsv.op("@@")(query)),
        )
        rows = await self.session.execute(
            self._visible(scope, statement, document_ids)
            .order_by(rank.desc(), DocumentChunk.id)
            .limit(k)
        )
        return [SearchHit(row.id, float(row.rank)) for row in rows]

    async def get_chunks(
        self, scope: OwnershipScope, chunk_ids: Collection[UUID], document_ids: Collection[UUID]
    ) -> list[ChunkRecord]:
        """Full records for chunks found by a search (same filters as the search itself)."""
        if not chunk_ids or not document_ids:
            return []
        return await self._records(scope, DocumentChunk.id.in_(list(chunk_ids)), document_ids)

    async def get_adjacent_chunks(
        self,
        scope: OwnershipScope,
        positions: Collection[tuple[UUID, int]],
        document_ids: Collection[UUID],
    ) -> list[ChunkRecord]:
        """The chunks at the given (document id, ordinal) positions: the neighbours of a hit."""
        if not positions or not document_ids:
            return []
        return await self._records(
            scope,
            tuple_(DocumentChunk.document_id, DocumentChunk.ordinal).in_(list(positions)),
            document_ids,
        )

    async def _records(
        self, scope: OwnershipScope, condition: Any, document_ids: Collection[UUID]
    ) -> list[ChunkRecord]:
        statement = cast(
            "Select[Any]",
            select(DocumentChunk, DocumentClause, Document.title)
            .join(Document, Document.id == DocumentChunk.document_id)
            .join(DocumentClause, DocumentClause.id == DocumentChunk.clause_id)
            .where(condition),
        )
        rows = await self.session.execute(self._visible(scope, statement, document_ids))
        return [
            ChunkRecord(
                chunk_id=chunk.id,
                clause_id=chunk.clause_id,
                document_id=chunk.document_id,
                document_title=title,
                ordinal=chunk.ordinal,
                text=chunk.text,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                clause_ref=clause.clause_ref,
                heading_path=list(clause.heading_path),
                clause_types=list(chunk.clause_types),
                injection_score=chunk.injection_score,
                has_table=bool(chunk.metadata_.get("has_table", False)),
            )
            for chunk, clause, title in rows.all()
        ]

    async def get_clauses(
        self, scope: OwnershipScope, clause_ids: Collection[UUID], document_ids: Collection[UUID]
    ) -> list[ClauseRecord]:
        """Whole clauses (the parents of retrieved chunks), for context expansion."""
        if not clause_ids or not document_ids:
            return []
        statement = cast(
            "Select[Any]",
            select(DocumentClause, Document.title)
            .join(Document, Document.id == DocumentClause.document_id)
            .where(DocumentClause.id.in_(list(clause_ids))),
        )
        scoped = scope.apply(statement, DocumentClause).where(
            Document.owner_id == scope.owner_id,
            Document.deleted_at.is_(None),
            Document.status == DocumentStatus.READY,
            DocumentClause.document_id.in_(list(document_ids)),
        )
        return [
            ClauseRecord(
                clause_id=clause.id,
                document_id=clause.document_id,
                document_title=title,
                clause_ref=clause.clause_ref,
                heading=clause.heading,
                heading_path=list(clause.heading_path),
                text=clause.text,
                page_start=clause.page_start,
                page_end=clause.page_end,
            )
            for clause, title in (await self.session.execute(scoped)).all()
        ]
