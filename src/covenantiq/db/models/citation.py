from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from covenantiq.core.ids import uuid7
from covenantiq.db.base import Base


class Citation(Base):
    """Evidence for exactly one parent: a chat message, a finding, or a vendor risk dimension.

    quote, document_title, page and clause_ref are snapshots, so a citation stays readable
    after its chunk has been purged.
    """

    __tablename__ = "citations"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(message_id, finding_id, risk_dimension_id) = 1", name="one_parent"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    finding_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("contract_findings.id", ondelete="CASCADE"), index=True
    )
    risk_dimension_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("vendor_risk_dimensions.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    chunk_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="SET NULL")
    )
    clause_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_clauses.id", ondelete="SET NULL")
    )
    marker: Mapped[int | None]
    quote: Mapped[str]
    document_title: Mapped[str]
    page_start: Mapped[int | None]
    page_end: Mapped[int | None]
    clause_ref: Mapped[str | None]
    relevance_score: Mapped[float | None]
    validated: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
