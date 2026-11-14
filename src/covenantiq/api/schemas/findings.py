from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from covenantiq.api.schemas.conversations import CitationOut
from covenantiq.api.schemas.jobs import JobOut
from covenantiq.core.enums import ClauseStatus, EvidenceSufficiency, RiskCategory, Severity


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ruleset: str | None = Field(default=None, description="Defaults to the current ruleset.")
    force: bool = Field(
        default=False,
        description="Analyse again even if a current analysis of this document exists.",
    )


class AnalyzeJob(JobOut):
    ruleset_version: str | None = None


class AnalyzeResponse(BaseModel):
    job: AnalyzeJob


class FindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    rule_id: str
    risk_category: RiskCategory
    clause_status: ClauseStatus
    severity: Severity
    title: str
    explanation: str
    business_impact: str | None
    recommended_action: str | None
    clause_ref: str | None
    page_number: int | None
    confidence: float
    evidence_sufficiency: EvidenceSufficiency
    evidence: list[CitationOut]
    details: dict[str, Any]
    analyzed_at: datetime


class FindingsResponse(BaseModel):
    document_id: UUID
    job_id: UUID
    ruleset_version: str | None
    analyzed_at: datetime | None
    summary: dict[str, int]
    notes: list[str]
    disclaimer: str
    findings: list[FindingOut]
