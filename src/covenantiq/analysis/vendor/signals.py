"""Mapping contract findings to signal observations (spec 21.2). Deterministic: no model."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from covenantiq.analysis.vendor.config_schema import SignalSpec
from covenantiq.analysis.vendor.types import EvidenceRef, Observation, SignalStatus
from covenantiq.core.enums import ClauseStatus, Severity

# the most risky evidence-based reading wins when one signal draws on several rules
_RISK_ORDER = {"favorable": 0, "ambiguous": 1, "unfavorable": 2}
_SEVERITY_RANK = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


@dataclass(frozen=True)
class FindingFacts:
    """What a signal needs from a contract finding."""

    rule_id: str
    status: ClauseStatus
    severity: Severity
    evidence: Sequence[EvidenceRef] = field(default_factory=tuple)


def unfavorable_severity(severity: Severity | None) -> Severity:
    """A finding's severity as one of the four an unfavorable signal can carry (`info` is `low`)."""
    if severity is None or severity is Severity.INFO:
        return Severity.LOW if severity is Severity.INFO else Severity.MEDIUM
    return severity


def risk_rank(status: str, severity: Severity | None) -> tuple[int, int]:
    """Orders readings from least to most risky: favorable, ambiguous, unfavorable by severity."""
    base = _RISK_ORDER.get(status, -1)
    return (
        base,
        _SEVERITY_RANK.get(unfavorable_severity(severity), 0) if status == "unfavorable" else 0,
    )


def _reading(fact: FindingFacts) -> tuple[SignalStatus, Severity | None] | None:
    """A finding as an evidence-based reading, or None if it is not one."""
    if fact.status is ClauseStatus.ACCEPTABLE:
        return "favorable", None
    if fact.status is ClauseStatus.PROBLEMATIC:
        return "unfavorable", unfavorable_severity(fact.severity)
    if fact.status is ClauseStatus.AMBIGUOUS:
        return "ambiguous", None
    return None


def observe_document(
    spec: SignalSpec, document_id: UUID, findings: Sequence[FindingFacts], *, analysis_failed: bool
) -> Observation:
    """One document's reading of one contract-rule signal."""
    if analysis_failed:
        return Observation(
            signal_id=spec.id,
            document_id=document_id,
            status="insufficient_evidence",
            note="the contract analysis of this document failed",
        )
    relevant = [f for f in findings if f.rule_id in spec.source.contract_rules]
    if not relevant:
        return Observation(
            signal_id=spec.id,
            document_id=document_id,
            status="insufficient_evidence",
            note="the contract analysis has no finding for this signal",
        )
    readings = [(r, f) for f in relevant if (r := _reading(f)) is not None]
    if readings:
        (status, severity), fact = max(readings, key=lambda rf: risk_rank(*rf[0]))
        evidence = [
            e for reading, f in readings if reading == (status, severity) for e in f.evidence
        ]
        return Observation(
            signal_id=spec.id,
            document_id=document_id,
            status=status,
            severity=severity,
            evidence=list(evidence or fact.evidence),
        )
    all_absent = all(f.status is ClauseStatus.ABSENT for f in relevant)
    return Observation(
        signal_id=spec.id,
        document_id=document_id,
        status="absent" if all_absent else "insufficient_evidence",
    )


def map_contract_signals(
    specs: Sequence[SignalSpec],
    document_ids: Sequence[UUID],
    findings_by_document: Mapping[UUID, Sequence[FindingFacts]],
    failed_documents: Collection[UUID] = (),
) -> list[Observation]:
    """Every contract-sourced signal, for every document."""
    return [
        observe_document(
            spec,
            document_id,
            findings_by_document.get(document_id, ()),
            analysis_failed=document_id in failed_documents,
        )
        for spec in specs
        if spec.from_contract
        for document_id in document_ids
    ]
