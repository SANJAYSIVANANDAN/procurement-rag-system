from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from covenantiq.core.enums import ContractType, DocumentStatus
from covenantiq.core.ids import uuid7
from covenantiq.db.base import Base, OwnedMixin, TimestampMixin
from covenantiq.db.models.clause import DocumentClause
from covenantiq.db.models.vendor import Vendor


class Document(Base, TimestampMixin, OwnedMixin):
    __tablename__ = "documents"
    __table_args__ = (CheckConstraint("size_bytes > 0", name="size_positive"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    vendor_id: Mapped[UUID | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"))
    title: Mapped[str]
    original_filename: Mapped[str]
    mime_type: Mapped[str]
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str]
    status: Mapped[DocumentStatus] = mapped_column(default=DocumentStatus.UPLOADED)
    contract_type: Mapped[ContractType] = mapped_column(default=ContractType.UNKNOWN)
    effective_date: Mapped[date | None]
    expiry_date: Mapped[date | None]
    page_count: Mapped[int | None]
    chunk_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    extraction_quality: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", default=dict, server_default=text("'{}'::jsonb")
    )
    error_code: Mapped[str | None]
    deleted_at: Mapped[datetime | None]

    vendor: Mapped[Vendor | None] = relationship(lazy="raise")
    clauses: Mapped[list[DocumentClause]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )


Index("ix_documents_owner_created", Document.owner_id, Document.created_at.desc())
Index("ix_documents_owner_vendor", Document.owner_id, Document.vendor_id)
Index("ix_documents_owner_status", Document.owner_id, Document.status)
# Duplicate detection: the same bytes may be uploaded again only after the earlier copy is deleted.
Index(
    "uq_documents_owner_sha256_active",
    Document.owner_id,
    Document.sha256,
    unique=True,
    postgresql_where=text("deleted_at IS NULL"),
)
