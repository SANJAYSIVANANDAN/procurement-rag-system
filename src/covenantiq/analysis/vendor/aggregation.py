"""Aggregating a signal across a vendor's documents (spec 21.4). Conservative, and independent of
the order of the documents."""

from collections.abc import Sequence

from covenantiq.analysis.vendor.config_schema import SignalSpec
from covenantiq.analysis.vendor.signals import risk_rank, unfavorable_severity
from covenantiq.analysis.vendor.types import (
    EVIDENCE_BASED,
    EvidenceRef,
    Observation,
    SignalAggregate,
)


def _evidence_key(e: EvidenceRef) -> tuple[str, str, str, str]:
    return (e.document_title, e.clause_ref or "", e.quote, str(e.document_id))


def aggregate_signal(spec: SignalSpec, observations: Sequence[Observation]) -> SignalAggregate:
    """The most risky evidence-based observation wins. With none, the signal is `absent` if every
    document says so and `insufficient_evidence` otherwise. Favorable in one document and
    unfavorable in another is flagged as a conflict (and still scored conservatively)."""
    mine = sorted(
        (o for o in observations if o.signal_id == spec.id),
        key=lambda o: (str(o.document_id), o.status, o.severity or ""),
    )
    based = [o for o in mine if o.status in EVIDENCE_BASED]
    if based:
        top = max(based, key=lambda o: risk_rank(o.status, o.severity))
        rank = risk_rank(top.status, top.severity)
        decisive = [o for o in based if risk_rank(o.status, o.severity) == rank]
        evidence = sorted(
            {_evidence_key(e): e for o in decisive for e in o.evidence}.values(), key=_evidence_key
        )
        conflict = any(o.status == "favorable" for o in based) and any(
            o.status == "unfavorable" for o in based
        )
        return SignalAggregate(
            signal_id=spec.id,
            dimension=spec.dimension,
            status=top.status,
            severity=unfavorable_severity(top.severity) if top.status == "unfavorable" else None,
            conflict=conflict,
            observations=mine,
            evidence=evidence,
        )
    all_absent = bool(mine) and all(o.status == "absent" for o in mine)
    return SignalAggregate(
        signal_id=spec.id,
        dimension=spec.dimension,
        status="absent" if all_absent else "insufficient_evidence",
        observations=mine,
    )


def aggregate_all(
    specs: Sequence[SignalSpec], observations: Sequence[Observation]
) -> dict[str, SignalAggregate]:
    return {spec.id: aggregate_signal(spec, observations) for spec in specs}
