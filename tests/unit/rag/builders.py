"""Small builders so rag tests read as the scenario they describe."""

from uuid import UUID, uuid4

from covenantiq.rag.types import (
    ContextSource,
    ParentClause,
    RetrievalResult,
    RetrievalTrace,
    RetrievedChunk,
)

DOC = UUID(int=1000)


def chunk(
    text: str = "Some contract text.",
    *,
    clause: UUID | None = None,
    ordinal: int = 0,
    rerank: float | None = None,
    dense: int | None = None,
    lexical: int | None = None,
    ref: str | None = "6.2",
    pages: tuple[int | None, int | None] = (4, 4),
    injection: float = 0.0,
    document: UUID = DOC,
    title: str = "Acme MSA",
    types: list[str] | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid4(),
        clause_id=clause or uuid4(),
        document_id=document,
        document_title=title,
        ordinal=ordinal,
        text=text,
        page_start=pages[0],
        page_end=pages[1],
        clause_ref=ref,
        heading_path=["Section"],
        clause_types=types or [],
        injection_score=injection,
        dense_rank=dense,
        lexical_rank=lexical,
        rerank_score=rerank,
    )


def parent(of: RetrievedChunk, text: str, tokens: int | None = None) -> ParentClause:
    return ParentClause(
        clause_id=of.clause_id,
        document_id=of.document_id,
        document_title=of.document_title,
        clause_ref=of.clause_ref,
        heading="Heading",
        heading_path=of.heading_path,
        text=text,
        token_count=tokens if tokens is not None else max(1, len(text) // 4),
        page_start=of.page_start,
        page_end=of.page_end,
    )


def result(
    chunks: list[RetrievedChunk],
    *,
    parents: list[ParentClause] | None = None,
    skipped: bool = False,
    neighbours: dict[UUID, list[RetrievedChunk]] | None = None,
) -> RetrievalResult:
    trace = RetrievalTrace(
        query_sha="x",
        keywords=[],
        fts_text="",
        embedding_model="m",
        document_count=1,
        rerank_skipped=skipped,
    )
    return RetrievalResult(
        chunks=chunks,
        parents={p.clause_id: p for p in parents or []},
        trace=trace,
        neighbours=neighbours or {},
    )


def source(
    marker: str,
    text: str,
    *,
    ref: str | None = "6.2",
    pages: tuple[int | None, int | None] = (4, 4),
    document: UUID = DOC,
) -> ContextSource:
    return ContextSource(
        marker=marker,
        chunk_ids=(uuid4(),),
        clause_id=uuid4(),
        document_id=document,
        document_title="Acme MSA",
        clause_ref=ref,
        page_start=pages[0],
        page_end=pages[1],
        text=text,
    )
