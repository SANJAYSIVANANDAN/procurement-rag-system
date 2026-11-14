from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from covenantiq.api.schemas.conversations import CitationOut
from covenantiq.api.schemas.jobs import JobOut
from covenantiq.core.enums import DimensionStatus, JobStatus, RiskDimension, Severity

DISCLAIMER = "Automated analysis for decision support. Not legal advice."


class AssessJob(JobOut):
    scoring_config_version: str | None = None
    pending_dependencies: list[str] = Field(default_factory=list)


class AssessResponse(BaseModel):
    job: AssessJob


class SignalOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    signal_id: str
    label: str
    weight: float
    status: str
    severity: Severity | None = None
    risk: float | None = None
    gap: str | None = None
    policy_treated: bool = False
    conflict: bool = False
    document_ids: list[UUID] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)


class MissingInfoOut(BaseModel):
    signal_id: str
    label: str
    reason: str


class DimensionOut(BaseModel):
    dimension: RiskDimension
    status: DimensionStatus
    score: float | None
    severity: Severity
    coverage: float
    reasoning: str | None
    signals: list[SignalOut]
    missing_information: list[MissingInfoOut]
    mitigations: list[str]
    evidence: list[CitationOut]


class DocumentRef(BaseModel):
    id: UUID
    title: str


class AssessmentOut(BaseModel):
    id: UUID
    vendor_id: UUID
    assessed_at: datetime
    scoring_config_version: str
    overall_score: float | None
    overall_severity: Severity
    evidence_coverage: float
    completeness: float
    provisional: bool
    weights: dict[str, float]
    effective_weights: dict[str, float] = Field(
        description="The weights actually applied: renormalised over the assessed dimensions."
    )
    documents_considered: list[DocumentRef]
    dimensions: list[DimensionOut]
    notes: list[str]
    disclaimer: str = DISCLAIMER


class CompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_ids: list[UUID] = Field(min_length=2, max_length=5)
    require_fresh: bool = True

    @field_validator("vendor_ids")
    @classmethod
    def unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("a vendor can appear only once")
        return value


class CompareResponse(BaseModel):
    comparison_id: UUID
    job: JobOut


class ComparisonOut(BaseModel):
    id: UUID
    status: JobStatus
    job: JobOut
    vendor_ids: list[str]
    assessment_ids: list[str]
    result: dict[str, Any] | None = Field(description="Empty until the job has succeeded.")
    narrative: str | None
    created_at: datetime
    disclaimer: str = DISCLAIMER
