"""What the model returns for vendor assessment and comparison (spec 17.4, 21.6).

Deliberately no numeric field anywhere: the model classifies and explains; scores, weights and
rankings come from the pure engine. A severity is a category the engine maps to a number.
"""

from typing import Literal

from pydantic import BaseModel, Field


class SignalExtraction(BaseModel):
    signal_id: str
    status: Literal["favorable", "unfavorable", "ambiguous", "absent", "insufficient_evidence"]
    unfavorable_severity: Literal["low", "medium", "high", "critical"] | None = None
    supporting_markers: list[int] = Field(
        default_factory=list, description="Numbers of the sources that support the status."
    )
    rationale: str = Field(description="At most 800 characters.")


class SignalExtractionBatch(BaseModel):
    extractions: list[SignalExtraction]


class Mitigation(BaseModel):
    text: str = Field(description="A review or negotiation step, at most 300 characters.")
    signal_ids: list[str] = Field(description="The unfavorable or ambiguous signals it addresses.")


class DimensionExplanation(BaseModel):
    reasoning: str = Field(description="At most 1200 characters; name signals by their id.")
    mitigations: list[Mitigation] = Field(default_factory=list)


class ComparisonNarrative(BaseModel):
    summary: str = Field(description="150 to 300 words.")
