from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from covenantiq.core.enums import DimensionStatus, RiskDimension, Severity
from covenantiq.core.ids import uuid7
from covenantiq.db.base import Base, OwnedMixin


class VendorRiskDimension(Base):
    __tablename__ = "vendor_risk_dimensions"
    __table_args__ = (UniqueConstraint("assessment_id", "dimension"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    assessment_id: Mapped[UUID] = mapped_column(
        ForeignKey("vendor_risk_assessments.id", ondelete="CASCADE")
    )
    dimension: Mapped[RiskDimension]
    score: Mapped[float | None]
    severity: Mapped[Severity]
    coverage: Mapped[float] = mapped_column(default=0.0, server_default=text("0"))
    status: Mapped[DimensionStatus]
    reasoning: Mapped[str | None]
    signals: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    missing_information: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    mitigations: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


class VendorRiskAssessment(Base, OwnedMixin):
    __tablename__ = "vendor_risk_assessments"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    vendor_id: Mapped[UUID] = mapped_column(
        ForeignKey("vendors.id", ondelete="CASCADE"), index=True
    )
    analysis_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("analysis_jobs.id", ondelete="CASCADE")
    )
    overall_score: Mapped[float | None]
    overall_severity: Mapped[Severity]
    evidence_coverage: Mapped[float] = mapped_column(default=0.0, server_default=text("0"))
    completeness: Mapped[float] = mapped_column(default=0.0, server_default=text("0"))
    provisional: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    is_latest: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    weights: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    scoring_config_version: Mapped[str]
    documents_considered: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    assessed_at: Mapped[datetime] = mapped_column(server_default=func.now())

    dimensions: Mapped[list[VendorRiskDimension]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )


# At most one latest assessment per vendor.
Index(
    "uq_vendor_risk_assessments_latest",
    VendorRiskAssessment.vendor_id,
    unique=True,
    postgresql_where=text("is_latest"),
)
