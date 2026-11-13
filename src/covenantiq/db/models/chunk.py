from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, ForeignKey, Index, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from covenantiq.core.ids import uuid7
from covenantiq.db.base import Base, OwnedMixin

EMBEDDING_DIM = 1024

TSV_EXPRESSION = (
    "setweight(to_tsvector('english', coalesce(metadata->>'heading_text', '')), 'A') || "
    "setweight(to_tsvector('english', text), 'B')"
)


class DocumentChunk(Base, OwnedMixin):
    """A child unit: the passage that is embedded, searched and cited."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal"),
        Index("ix_document_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_document_chunks_owner_document", "owner_id", "document_id"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    clause_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_clauses.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int]
    text: Mapped[str]
    token_count: Mapped[int]
    page_start: Mapped[int]
    page_end: Mapped[int]
    clause_types: Mapped[list[str]] = mapped_column(default=list, server_default=sql_text("'{}'"))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None]
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed(TSV_EXPRESSION, persisted=True))
    injection_score: Mapped[float] = mapped_column(default=0.0, server_default=sql_text("0"))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", default=dict, server_default=sql_text("'{}'::jsonb")
    )
