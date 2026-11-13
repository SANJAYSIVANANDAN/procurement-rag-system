from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from covenantiq.core.enums import (
    AnalysisJobType,
    ClauseStatus,
    EvidenceSufficiency,
    JobStatus,
    ReviewStatus,
    RiskCategory,
    Severity,
)
from covenantiq.core.ids import uuid7
from covenantiq.db.base import Base, OwnedMixin


class AnalysisJob(Base, OwnedMixin):
    __tablename__ = "analysis_jobs"
    __table_args__ = (
        CheckConstraint(
            "(job_type = 'contract_analysis' AND document_id IS NOT NULL) "
            "OR (job_type = 'vendor_assessment' AND vendor_id IS NOT NULL) "
            "OR job_type = 'vendor_comparison'",
            name="target_matches_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    job_type: Mapped[AnalysisJobType]
    status: Mapped[JobStatus] = mapped_column(default=JobStatus.QUEUED)
    document_id: Mapped[UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    vendor_id: Mapped[UUID | None] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    ruleset_version: Mapped[str | None]
    scoring_config_version: Mapped[str | None]
    celery_task_id: Mapped[str | None]
    attempt: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    progress: Mapped[float] = mapped_column(default=0.0, server_default=text("0"))
    stage: Mapped[str | None]
    error_code: Mapped[str | None]
    heartbeat_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]


Index(
    "ix_analysis_jobs_owner_type_created",
    AnalysisJob.owner_id,
    AnalysisJob.job_type,
    AnalysisJob.created_at.desc(),
)


class ContractFinding(Base, OwnedMixin):
    __tablename__ = "contract_findings"
    __table_args__ = (UniqueConstraint("analysis_job_id", "rule_id", "ordinal"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    analysis_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("analysis_jobs.id", ondelete="CASCADE")
    )
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    rule_id: Mapped[str]
    ordinal: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    risk_category: Mapped[RiskCategory]
    clause_status: Mapped[ClauseStatus]
    severity: Mapped[Severity]
    title: Mapped[str]
    explanation: Mapped[str]
    business_impact: Mapped[str | None]
    recommended_action: Mapped[str | None]
    clause_ref: Mapped[str | None]
    page_number: Mapped[int | None]
    confidence: Mapped[float | None]
    evidence_sufficiency: Mapped[EvidenceSufficiency]
    review_status: Mapped[ReviewStatus] = mapped_column(
        default=ReviewStatus.UNREVIEWED, server_default=ReviewStatus.UNREVIEWED.value
    )
    related_document_ids: Mapped[list[Any] | None] = mapped_column(JSONB)
    details: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    analyzed_at: Mapped[datetime] = mapped_column(server_default=func.now())


Index(
    "ix_contract_findings_document_job",
    ContractFinding.document_id,
    ContractFinding.analysis_job_id,
)
Index("ix_contract_findings_owner_severity", ContractFinding.owner_id, ContractFinding.severity)
