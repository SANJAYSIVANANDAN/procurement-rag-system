"""The data that flows through a contract analysis.

Everything here is a plain Pydantic model that round-trips through JSON, because the graph's state
is checkpointed to PostgreSQL after every step and a resumed run must read it back exactly.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from covenantiq.core.enums import ClauseStatus, EvidenceSufficiency, RiskCategory, Severity


class SourceRef(BaseModel):
    """A passage shown to the model as `[C<marker>]`, kept only if something cites it."""

    marker: int
    chunk_ids: list[UUID] = Field(default_factory=list)
    clause_id: UUID | None = None
    document_id: UUID
    document_title: str
    clause_ref: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    text: str
    rerank_score: float | None = None


class HeuristicHit(BaseModel):
    rule_id: str
    heuristic_id: str
    marker: int | None = None  # the source it was found in
    chunk_id: UUID | None = None
    span: str  # the matched text, shortened
    status: Literal["problematic"] | None = None
    severity: Severity | None = None
    flag: str | None = None


class ValueObservation(BaseModel):
    """A value seen in a source, for spotting two clauses that disagree."""

    key: str
    value: str  # normalised: "30" (days), "england and wales"
    raw: str  # as written
    unit: Literal["days", "hours", "text"] = "text"
    rule_id: str
    source: SourceRef


class RuleResult(BaseModel):
    rule_id: str
    title: str
    category: RiskCategory
    status: ClauseStatus
    severity: Severity
    confidence: float
    evidence_sufficiency: EvidenceSufficiency
    explanation: str
    business_impact: str | None = None
    recommended_action: str | None = None
    clause_ref: str | None = None
    page_number: int | None = None
    markers: list[int] = Field(default_factory=list)  # validated
    sources: list[SourceRef] = Field(default_factory=list)  # the cited ones
    red_flags: list[str] = Field(default_factory=list)
    heuristic_hits: list[HeuristicHit] = Field(default_factory=list)
    extracted_values: dict[str, str | None] = Field(default_factory=dict)
    observations: list[ValueObservation] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)  # why this status: for the details and logs
    search_trace: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None  # set when the assessment could not be made (LLM down, retrieval)


class ObligationRecord(BaseModel):
    party: str
    action: str
    deadline_text: str | None = None
    deadline_days: int | None = None
    trigger: str | None = None
    clause_ref: str | None = None
    page_number: int | None = None
    source: SourceRef


class ContradictionRecord(BaseModel):
    key: str
    first: ValueObservation
    second: ValueObservation
    explanation: str


class FindingDraft(BaseModel):
    """One finding, ready to store: the row's fields plus its evidence."""

    rule_id: str
    ordinal: int = 0
    risk_category: RiskCategory
    clause_status: ClauseStatus
    severity: Severity
    title: str
    explanation: str
    business_impact: str | None = None
    recommended_action: str | None = None
    clause_ref: str | None = None
    page_number: int | None = None
    confidence: float
    evidence_sufficiency: EvidenceSufficiency
    evidence: list[SourceRef] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
