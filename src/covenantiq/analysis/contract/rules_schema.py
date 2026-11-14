"""The shape of a contract ruleset (spec 20.3). Invalid rules fail at start-up, not mid-analysis."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from covenantiq.core.enums import ContractType, RiskCategory, Severity

SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.INFO,
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
)

ContractTypeName = Literal[
    "all", "msa", "saas", "dpa", "sow", "nda", "license", "supply", "services"
]
HeuristicStatus = Literal["problematic"]  # a heuristic can only ever push towards risk


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HeuristicEffect(_Strict):
    status: HeuristicStatus | None = None
    severity: Severity | None = None
    flag: str | None = None

    @model_validator(mode="after")
    def _does_something(self) -> "HeuristicEffect":
        if self.status is None and self.severity is None and self.flag is None:
            raise ValueError("a heuristic effect must set a status, a severity or a flag")
        return self


class Heuristic(_Strict):
    id: str
    pattern: str
    effect: HeuristicEffect

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        return value


class Compare(_Strict):
    """A value to look for in the rule's sources, to detect internal contradictions."""

    key: str
    pattern: str  # the first group is the value
    unit: Literal["days", "hours", "text"] = "text"

    @field_validator("pattern")
    @classmethod
    def _has_a_group(cls, value: str) -> str:
        try:
            compiled = re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        if compiled.groups < 1:
            raise ValueError("a compare pattern needs a capture group for the value")
        return value


class Retrieval(_Strict):
    queries: Annotated[list[str], Field(min_length=1, max_length=4)]
    keywords: list[str] = Field(default_factory=list)


class SeverityPolicy(_Strict):
    absent: Severity
    problematic_default: Severity
    ambiguous: Severity


class RuleSpec(_Strict):
    id: Annotated[str, Field(pattern=r"^[A-Z]{2,5}-\d{3}$")]
    version: int = 1
    title: str
    category: RiskCategory
    applies_to: Annotated[list[ContractTypeName], Field(min_length=1)]
    excludes: list[ContractTypeName] = Field(default_factory=list)
    clause_types: list[str] = Field(default_factory=list)
    retrieval: Retrieval
    heuristics: list[Heuristic] = Field(default_factory=list)
    extract: dict[str, str] = Field(default_factory=dict)
    assessment_questions: Annotated[list[str], Field(min_length=1)]
    severity_policy: SeverityPolicy
    recommended_actions: dict[Literal["absent", "problematic", "ambiguous"], str]
    compare: list[Compare] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_heuristics(self) -> "RuleSpec":
        ids = [h.id for h in self.heuristics]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.id}: duplicate heuristic id")
        return self

    def applies(self, contract_type: ContractType) -> bool:
        """Does this rule apply to a contract of this type? An unset type gets the rules for
        every contract plus those for an MSA (spec 20.2)."""
        kind = "msa" if contract_type is ContractType.UNKNOWN else contract_type.value
        if kind in self.excludes:
            return False
        return "all" in self.applies_to or kind in self.applies_to


class UnusualProvision(_Strict):
    id: Annotated[str, Field(pattern=r"^[A-Z_]{3,30}$")]
    title: str
    category: RiskCategory
    pattern: str
    severity: Severity
    explanation: str
    recommended_action: str

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        return value

    @property
    def rule_id(self) -> str:
        return f"UNUSUAL-{self.id}"


class RulesFile(_Strict):
    ruleset: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    semver: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    rules: Annotated[list[RuleSpec], Field(min_length=1)]
    unusual: list[UnusualProvision] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "RulesFile":
        ids = [r.id for r in self.rules] + [u.rule_id for u in self.unusual]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate rule id(s): {', '.join(duplicates)}")
        return self

    @property
    def version(self) -> str:
        """`contract_baseline_v1@1.0.0`: stored on every analysis job."""
        return f"{self.ruleset}@{self.semver}"
