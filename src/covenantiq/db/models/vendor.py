from typing import Any
from uuid import UUID

from sqlalchemy import Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from covenantiq.core.ids import uuid7
from covenantiq.db.base import Base, OwnedMixin, TimestampMixin


class Vendor(Base, TimestampMixin, OwnedMixin):
    __tablename__ = "vendors"
    __table_args__ = (
        UniqueConstraint("owner_id", "normalized_name"),
        Index(
            "ix_vendors_normalized_name_trgm",
            "normalized_name",
            postgresql_using="gin",
            postgresql_ops={"normalized_name": "gin_trgm_ops"},
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str]
    normalized_name: Mapped[str]
    industry: Mapped[str | None]
    website: Mapped[str | None]
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", default=dict, server_default=text("'{}'::jsonb")
    )
