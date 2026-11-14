from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from covenantiq.core.ids import uuid7
from covenantiq.db.base import Base, OwnedMixin


class DocumentClause(Base, OwnedMixin):
    """A parent unit: one clause or section of a contract (spec 15.3)."""

    __tablename__ = "document_clauses"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal"),
        Index("ix_document_clauses_clause_types", "clause_types", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int]
    clause_ref: Mapped[str | None]
    heading: Mapped[str | None]
    heading_path: Mapped[list[str]] = mapped_column(default=list, server_default=sql_text("'{}'"))
    clause_types: Mapped[list[str]] = mapped_column(default=list, server_default=sql_text("'{}'"))
    page_start: Mapped[int | None]
    page_end: Mapped[int | None]
    text: Mapped[str]
    has_table: Mapped[bool] = mapped_column(default=False, server_default=sql_text("false"))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", default=dict, server_default=sql_text("'{}'::jsonb")
    )
