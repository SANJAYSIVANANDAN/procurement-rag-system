"""The vendor signal taxonomy and scoring configuration (spec 21.2, 21.3), validated at load time.

An invalid configuration stops the process: a silently wrong weight would change every score.
"""

import math
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from covenantiq.core.enums import RiskDimension, Severity

DEFAULT_SIGNALS_PATH = Path("config/vendor/signals_v1.yaml")
DEFAULT_SCORING_PATH = Path("config/vendor/scoring_v1.yaml")
UnfavorableSeverity = Literal["low", "medium", "high", "critical"]
VERSION = r"^[a-z0-9_]+@\d+\.\d+\.\d+$"


class VendorConfigError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---- signals --------------------------------------------------------------------------------


class ExtractionSource(_Strict):
    queries: Annotated[list[str], Field(min_length=1, max_length=4)]
    question: str


class SignalSource(_Strict):
    contract_rules: list[str] = Field(default_factory=list)
    extraction: ExtractionSource | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "SignalSource":
        if bool(self.contract_rules) == (self.extraction is not None):
            raise ValueError(
                "a signal has either contract_rules or extraction, not both or neither"
            )
        return self


class SignalSpec(_Strict):
    id: Annotated[str, Field(pattern=r"^[A-Z]{2,4}-[A-Z0-9-]+$")]
    dimension: RiskDimension
    label: str
    weight: Annotated[float, Field(gt=0)]
    source: SignalSource

    @property
    def from_contract(self) -> bool:
        return bool(self.source.contract_rules)

    def extraction(self) -> ExtractionSource:
        """The queries and question of an extraction signal."""
        if self.source.extraction is None:
            raise ValueError(f"{self.id} is not an extraction signal")
        return self.source.extraction


class SignalsConfig(_Strict):
    version: Annotated[str, Field(pattern=VERSION)]
    signals: Annotated[list[SignalSpec], Field(min_length=1)]

    @model_validator(mode="after")
    def _consistent(self) -> "SignalsConfig":
        ids = [s.id for s in self.signals]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate signal id(s): {', '.join(duplicates)}")
        missing = set(RiskDimension) - {s.dimension for s in self.signals}
        if missing:
            raise ValueError(
                "no signal for dimension(s): " + ", ".join(sorted(d.value for d in missing))
            )
        return self

    def by_id(self) -> dict[str, SignalSpec]:
        return {s.id: s for s in self.signals}

    def for_dimension(self, dimension: RiskDimension) -> list[SignalSpec]:
        return [s for s in self.signals if s.dimension is dimension]


# ---- scoring --------------------------------------------------------------------------------


class UnfavorableRisk(_Strict):
    low: float
    medium: float
    high: float
    critical: float


class SignalRisk(_Strict):
    favorable: float
    ambiguous: float
    unfavorable: UnfavorableRisk

    @model_validator(mode="after")
    def _ordered(self) -> "SignalRisk":
        u = self.unfavorable
        chain = [self.favorable, self.ambiguous, u.low, u.medium, u.high, u.critical]
        if any(not 0 <= v <= 100 for v in chain):
            raise ValueError("risk values must be between 0 and 100")
        if self.favorable > min(self.ambiguous, u.low):
            raise ValueError("a favorable signal must never be riskier than any other status")
        if not u.low <= u.medium <= u.high <= u.critical:
            raise ValueError("unfavorable risk must not decrease with severity")
        return self


class AbsentOverride(_Strict):
    treat_as: Literal["unfavorable"]
    severity: UnfavorableSeverity
    rationale: str


class AbsentPolicy(_Strict):
    default: Literal["gap"] = "gap"
    overrides: dict[str, AbsentOverride] = Field(default_factory=dict)


class SeverityBands(_Strict):
    low: float
    medium: float
    high: float

    @model_validator(mode="after")
    def _increasing(self) -> "SeverityBands":
        if not 0 < self.low < self.medium < self.high <= 100:
            raise ValueError("severity band bounds must increase and stay within 0 to 100")
        return self


class ComparisonConfig(_Strict):
    min_coverage_to_compare: Annotated[float, Field(ge=0, le=1)]
    min_comparable_dimensions_to_rank: Annotated[int, Field(ge=1, le=6)] = 4


class ScoringConfig(_Strict):
    version: Annotated[str, Field(pattern=VERSION)]
    dimension_weights: dict[RiskDimension, Annotated[float, Field(gt=0)]]
    signal_risk: SignalRisk
    absent_policy: AbsentPolicy
    min_dimension_coverage: Annotated[float, Field(ge=0, le=1)]
    provisional_completeness_threshold: Annotated[float, Field(ge=0, le=1)]
    min_assessed_dimensions: Annotated[int, Field(ge=1, le=6)] = 4
    severity_bands: SeverityBands
    comparison: ComparisonConfig

    @model_validator(mode="after")
    def _weights(self) -> "ScoringConfig":
        if set(self.dimension_weights) != set(RiskDimension):
            raise ValueError("dimension_weights must name all six dimensions")
        if not math.isclose(sum(self.dimension_weights.values()), 1.0, abs_tol=1e-9):
            raise ValueError(
                f"dimension weights must sum to 1.0, got {sum(self.dimension_weights.values()):.4f}"
            )
        return self

    def band(self, score: float | None) -> Severity:
        """The severity of a 0 to 100 risk score. No score means nothing to rate: `info`."""
        if score is None:
            return Severity.INFO
        bands = self.severity_bands
        if score < bands.low:
            return Severity.LOW
        if score < bands.medium:
            return Severity.MEDIUM
        if score < bands.high:
            return Severity.HIGH
        return Severity.CRITICAL


class VendorConfig(_Strict):
    """Both files, checked against each other."""

    signals: SignalsConfig
    scoring: ScoringConfig


# ---- loading --------------------------------------------------------------------------------


def _read(path: Path) -> object:
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise VendorConfigError(f"cannot read {path}: {exc}") from exc


def _validate[M: BaseModel](model: type[M], raw: object, path: Path) -> M:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8]
        )
        raise VendorConfigError(f"invalid {path}: {details}") from exc


def load_vendor_config(
    signals_path: Path | None = None,
    scoring_path: Path | None = None,
    *,
    contract_rule_ids: set[str] | None = None,
) -> VendorConfig:
    signals_file = signals_path or DEFAULT_SIGNALS_PATH
    scoring_file = scoring_path or DEFAULT_SCORING_PATH
    signals = _validate(SignalsConfig, _read(signals_file), signals_file)
    scoring = _validate(ScoringConfig, _read(scoring_file), scoring_file)

    problems = [
        f"absent override names an unknown signal: {signal_id}"
        for signal_id in scoring.absent_policy.overrides
        if signal_id not in signals.by_id()
    ]
    if contract_rule_ids is not None:
        problems += [
            f"{s.id}: unknown contract rule {rule}"
            for s in signals.signals
            for rule in s.source.contract_rules
            if rule not in contract_rule_ids
        ]
    if problems:
        raise VendorConfigError("; ".join(problems))
    return VendorConfig(signals=signals, scoring=scoring)


@lru_cache(maxsize=1)
def get_vendor_config() -> VendorConfig:
    """The shipped configuration, checked against the shipped contract ruleset."""
    from covenantiq.analysis.contract.rules_loader import get_ruleset

    return load_vendor_config(contract_rule_ids={r.id for r in get_ruleset().file.rules})
