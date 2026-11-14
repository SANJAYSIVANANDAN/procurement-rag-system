"""The deterministic scoring engine (spec 21.5). Pure: no model, no I/O, no clock.

Scores are risks from 0 to 100, higher is riskier. Absence is a gap, not a score, unless the
configuration names the signal as one where absence itself is a risk. Gaps lower coverage and
completeness and never move a score. Dimension weights are renormalised over the dimensions that
were assessed, and that is disclosed (`assessed_dimensions`).

Properties the tests enforce: a favorable signal never raises a dimension's score; gaps never
change it; the order of documents does not matter; the same input gives the same output.
"""

from collections.abc import Mapping

from covenantiq.analysis.vendor.config_schema import ScoringConfig, SignalsConfig, SignalSpec
from covenantiq.analysis.vendor.signals import unfavorable_severity
from covenantiq.analysis.vendor.types import (
    AssessmentScore,
    DimensionScore,
    MissingInformation,
    SignalAggregate,
    SignalScore,
)
from covenantiq.core.enums import DimensionStatus, RiskDimension, Severity


def signal_risk(config: ScoringConfig, status: str, severity: Severity | None) -> float:
    risk = config.signal_risk
    if status == "favorable":
        return risk.favorable
    if status == "ambiguous":
        return risk.ambiguous
    level = unfavorable_severity(severity).value
    return float(getattr(risk.unfavorable, level))


def score_signal(
    config: ScoringConfig, spec: SignalSpec, aggregate: SignalAggregate | None
) -> SignalScore:
    """One signal's effective status, risk and, if it has none, why it is a gap."""
    status = aggregate.status if aggregate is not None else "insufficient_evidence"
    severity = aggregate.severity if aggregate is not None else None
    document_ids = (
        sorted({o.document_id for o in aggregate.observations}, key=str) if aggregate else []
    )
    base = {
        "signal_id": spec.id,
        "label": spec.label,
        "weight": spec.weight,
        "conflict": bool(aggregate and aggregate.conflict),
        "document_ids": document_ids,
    }
    policy_treated = False
    if status == "absent":
        override = config.absent_policy.overrides.get(spec.id)
        if override is None:
            return SignalScore(**base, status="absent", gap="absent_in_all_documents")
        status, severity, policy_treated = "unfavorable", Severity(override.severity), True
    if status == "insufficient_evidence":
        return SignalScore(**base, status="insufficient_evidence", gap="insufficient_evidence")
    return SignalScore(
        **base,
        status=status,
        severity=severity,
        risk=signal_risk(config, status, severity),
        policy_treated=policy_treated,
    )


def score_dimension(
    config: ScoringConfig,
    dimension: RiskDimension,
    specs: list[SignalSpec],
    aggregates: Mapping[str, SignalAggregate],
) -> DimensionScore:
    scored = [score_signal(config, spec, aggregates.get(spec.id)) for spec in specs]
    included = [s for s in scored if s.risk is not None]
    total_weight = sum(s.weight for s in scored)
    included_weight = sum(s.weight for s in included)
    coverage = included_weight / total_weight if total_weight else 0.0
    score: float | None = None
    if included:
        score = sum(s.weight * (s.risk or 0.0) for s in included) / included_weight
    if not included:
        status = DimensionStatus.NOT_ASSESSED
    elif coverage < config.min_dimension_coverage:
        status = DimensionStatus.INSUFFICIENT_EVIDENCE
    else:
        status = DimensionStatus.ASSESSED
    return DimensionScore(
        dimension=dimension,
        status=status,
        score=None if score is None else round(score, 4),
        severity=config.band(score),
        coverage=round(coverage, 4),
        signals=scored,
        missing_information=[
            MissingInformation(signal_id=s.signal_id, label=s.label, reason=s.gap)
            for s in scored
            if s.gap is not None
        ],
    )


def score_assessment(
    config: ScoringConfig, taxonomy: SignalsConfig, aggregates: Mapping[str, SignalAggregate]
) -> AssessmentScore:
    dimensions = [
        score_dimension(config, dimension, taxonomy.for_dimension(dimension), aggregates)
        for dimension in RiskDimension
    ]
    weights = config.dimension_weights
    assessed = [d for d in dimensions if d.status is DimensionStatus.ASSESSED]
    overall: float | None = None
    if assessed:
        weight_sum = sum(weights[d.dimension] for d in assessed)
        overall = sum(weights[d.dimension] * (d.score or 0.0) for d in assessed) / weight_sum
    every_signal = [s for d in dimensions for s in d.signals]
    evidence_coverage = (
        sum(1 for s in every_signal if s.risk is not None) / len(every_signal)
        if every_signal
        else 0.0
    )
    completeness = sum(weights[d.dimension] * d.coverage for d in dimensions)
    return AssessmentScore(
        overall_score=None if overall is None else round(overall, 4),
        overall_severity=config.band(overall),
        evidence_coverage=round(evidence_coverage, 4),
        completeness=round(completeness, 4),
        provisional=(
            completeness < config.provisional_completeness_threshold
            or len(assessed) < config.min_assessed_dimensions
        ),
        dimensions=dimensions,
        assessed_dimensions=[d.dimension for d in assessed],
    )


def effective_weights(
    config: ScoringConfig, assessed: list[RiskDimension]
) -> dict[RiskDimension, float]:
    """The weights actually applied: the configured ones renormalised over assessed dimensions."""
    total = sum(config.dimension_weights[d] for d in assessed)
    return {d: config.dimension_weights[d] / total for d in assessed} if total else {}
