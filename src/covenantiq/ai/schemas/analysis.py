"""What the model returns for contract analysis (spec 17.4).

The model gives categories (a confidence of high, medium or low; a status), never numbers: scores,
severities and confidence values are computed in code. Length limits are in the field descriptions
and enforced by truncation afterwards, not by the schema, because a reply that is a little too
long should not be thrown away.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ExtractedValue(BaseModel):
    """One value the rule asked for. A list of pairs rather than a free-form object, because
    structured output needs a closed schema."""

    name: str
    value: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _text(cls, raw: Any) -> Any:
        """A number or a yes/no is the value as the contract states it, written as text: some
        models return `30` where others return `"30"`."""
        if isinstance(raw, bool):
            return "yes" if raw else "no"
        if isinstance(raw, (int, float)):
            return str(raw)
        return raw


class RuleAssessment(BaseModel):
    rule_id: str
    clause_found: Literal["yes", "no", "unclear"]
    supporting_markers: list[int] = Field(
        default_factory=list, description="Numbers of the sources that support the answer."
    )
    status: Literal["problematic", "ambiguous", "acceptable", "absent", "insufficient_evidence"]
    red_flags: list[str] = Field(default_factory=list)
    extracted_values: list[ExtractedValue] = Field(default_factory=list)
    explanation: str = Field(description="At most 1200 characters.")
    business_impact: str = Field(default="", description="At most 800 characters.")
    recommended_action: str = Field(default="", description="At most 600 characters.")
    confidence: Literal["high", "medium", "low"]


class RuleAssessmentBatch(BaseModel):
    assessments: list[RuleAssessment]


class ObligationItem(BaseModel):
    party: str = Field(description="Who must act: customer, vendor, or both.")
    action: str
    deadline_text: str | None = Field(default=None, description="The deadline as written.")
    deadline_days: int | None = Field(
        default=None, description="The deadline in days, only if it is stated as a number of days."
    )
    trigger: str | None = None
    clause_ref: str | None = None
    marker: int = Field(description="Number of the source the obligation comes from.")


class ObligationExtraction(BaseModel):
    obligations: list[ObligationItem]


class ContradictionVerdict(BaseModel):
    is_contradiction: bool
    explanation: str = Field(description="At most 500 characters.")
