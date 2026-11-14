from dataclasses import dataclass

from covenantiq.ingestion.chunking.chunker import ChunkDraft, ChunkSettings, chunk_clause
from covenantiq.ingestion.chunking.clause_builder import (
    ClauseDraft,
    build_clauses,
    choose_heading_depth,
)
from covenantiq.ingestion.extraction.types import ExtractedBlock
from covenantiq.ingestion.taxonomy import Taxonomy

HEADER_SEPARATOR = " | "
PATH_SEPARATOR = " > "


@dataclass
class ChunkedDocument:
    clauses: list[ClauseDraft]
    chunks: list[ChunkDraft]


def context_header(title: str, clause: ClauseDraft) -> str:
    """`{title} | {heading path} | clause {ref}`: prefixed to a chunk for embedding and search so
    a passage carries where it sits. The stored chunk text does not include it."""
    parts = [title]
    if clause.heading_path:
        parts.append(PATH_SEPARATOR.join(clause.heading_path))
    if clause.clause_ref:
        parts.append(f"clause {clause.clause_ref}")
    return HEADER_SEPARATOR.join(parts)


def chunk_document(
    blocks: list[ExtractedBlock],
    *,
    title: str,
    taxonomy: Taxonomy,
    parent_max_tokens: int = 1500,
    heading_depth: int | None = None,
    settings: ChunkSettings | None = None,
) -> ChunkedDocument:
    """Blocks in, parent clauses and child chunks out (spec 18.6)."""
    settings = settings or ChunkSettings(target_tokens=350)
    depth = choose_heading_depth(blocks, heading_depth)
    clauses = build_clauses(
        blocks, heading_depth=depth, parent_max_tokens=parent_max_tokens, taxonomy=taxonomy
    )
    chunks: list[ChunkDraft] = []
    for clause in clauses:
        header = context_header(title, clause)
        for chunk in chunk_clause(clause, settings):
            chunk.ordinal = len(chunks)
            chunk.header = header
            chunks.append(chunk)
    return ChunkedDocument(clauses=clauses, chunks=chunks)
