"""The data of a vendor assessment. Plain Pydantic models: they round-trip through JSON because the
graph's state is checkpointed and the results are stored."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from covenantiq.core.enums import DimensionStatus, RiskDimension, Severity

SignalStatus = Literal["favorable", "unfavorable", "ambiguous", "absent", "insufficient_evidence"]
EVIDENCE_BASED: frozenset[str] = frozenset({"favorable", "unfavorable", "ambiguous"})
GapReason = Literal["absent_in_all_documents", "insufficient_evidence"]


class EvidenceRef(BaseModel):
    document_id: UUID
    document_title: str
    quote: str
    page_start: int | None = None
    page_end: int | None = None
    clause_ref: str | None = None
    chunk_id: UUID | None = None
    clause_id: UUID | None = None


class Observation(BaseModel):
    """What one document says about one signal."""

    signal_id: str
    document_id: UUID
    status: SignalStatus
    severity: Severity | None = None  # only for unfavorable: low, medium, high or critical
    evidence: list[EvidenceRef] = Field(default_factory=list)
    note: str | None = None


class SignalAggregate(BaseModel):
    """What the vendor's documents say about one signal, taken together (spec 21.4)."""

    signal_id: str
    dimension: RiskDimension
    status: SignalStatus
    severity: Severity | None = None
    conflict: bool = False
    observations: list[Observation] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class SignalScore(BaseModel):
    signal_id: str
    label: str
    weight: float
    status: SignalStatus  # after the absent policy
    severity: Severity | None = None
    risk: float | None = None  # None for a gap
    gap: GapReason | None = None
    policy_treated: bool = False  # an absence counted as unfavorable by explicit policy
    conflict: bool = False
    document_ids: list[UUID] = Field(default_factory=list)


class MissingInformation(BaseModel):
    signal_id: str
    label: str
    reason: GapReason


class DimensionScore(BaseModel):
    dimension: RiskDimension
    status: DimensionStatus
    score: float | None
    severity: Severity
    coverage: float
    signals: list[SignalScore]
    missing_information: list[MissingInformation]


class AssessmentScore(BaseModel):
    overall_score: float | None
    overall_severity: Severity
    evidence_coverage: float
    completeness: float
    provisional: bool
    dimensions: list[DimensionScore]
    assessed_dimensions: list[RiskDimension]
