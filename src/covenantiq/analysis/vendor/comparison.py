"""Comparing vendors (spec 21.8). Pure and deterministic.

The rule that shapes everything here: *an absence is never ranked as better or worse than an
established clause*, unless the configuration says absence is itself unfavorable for that signal
(and then it is labelled as such). Two vendors are compared on a dimension or a signal only where
both have evidence; otherwise the cell says what is missing. A ranking is produced only when every
assessment is complete enough to deserve one.
"""

import re
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from covenantiq.analysis.vendor.config_schema import ScoringConfig
from covenantiq.analysis.vendor.types import EVIDENCE_BASED
from covenantiq.core.enums import DimensionStatus, RiskDimension, Severity

POLICY_LABEL = "absent (treated as unfavorable by policy)"
WORDS_MIN, WORDS_MAX = 100, 350
_COMPARATIVE = re.compile(
    r"\b(better|worse|lower|higher|safer|riskier|less risky|more risky|outperform\w*|ahead of|"
    r"falls? behind|trails?|stronger|weaker|leads?|leader|best|worst|ranks?|ranked|"
    r"preferable|superior|inferior)\b",
    re.IGNORECASE,
)
_SIGNAL_ID = re.compile(r"\b[A-Z]{2,4}-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")


class DimensionSnapshot(BaseModel):
    dimension: RiskDimension
    status: DimensionStatus
    score: float | None
    severity: Severity
    coverage: float


class SignalSnapshot(BaseModel):
    signal_id: str
    dimension: RiskDimension
    label: str
    status: str  # favorable, unfavorable, ambiguous, absent, insufficient_evidence
    policy_treated: bool = False  # an absence the configuration counts as unfavorable


class AssessmentSnapshot(BaseModel):
    vendor_id: UUID
    vendor_name: str
    assessment_id: UUID
    overall_score: float | None
    provisional: bool
    dimensions: list[DimensionSnapshot]
    signals: list[SignalSnapshot] = Field(default_factory=list)


def _dimension_cell(
    snapshot: AssessmentSnapshot, dimension: RiskDimension
) -> DimensionSnapshot | None:
    return next((d for d in snapshot.dimensions if d.dimension is dimension), None)


def _comparable_dimension(
    config: ScoringConfig, cells: dict[UUID, DimensionSnapshot | None], names: dict[UUID, str]
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for vendor_id, cell in cells.items():
        if cell is None or cell.status is not DimensionStatus.ASSESSED:
            reasons.append(f"{names[vendor_id]}: not assessed")
        elif cell.coverage < config.comparison.min_coverage_to_compare:
            reasons.append(f"{names[vendor_id]}: coverage {cell.coverage:.2f}")
    return not reasons, reasons


def compare(config: ScoringConfig, snapshots: Sequence[AssessmentSnapshot]) -> dict[str, Any]:
    """The comparison matrix for two or more assessments made with the same configuration."""
    ordered = sorted(snapshots, key=lambda s: (s.vendor_name.lower(), str(s.vendor_id)))
    names = {s.vendor_id: s.vendor_name for s in ordered}
    ids = [str(s.vendor_id) for s in ordered]

    dimensions: list[dict[str, Any]] = []
    for dimension in RiskDimension:
        cells = {s.vendor_id: _dimension_cell(s, dimension) for s in ordered}
        comparable, reasons = _comparable_dimension(config, cells, names)
        row: dict[str, Any] = {
            "dimension": dimension.value,
            "comparable": comparable,
            "cells": {
                str(vendor_id): None
                if cell is None
                else {
                    "score": cell.score,
                    "severity": cell.severity.value,
                    "coverage": cell.coverage,
                    "status": cell.status.value,
                }
                for vendor_id, cell in cells.items()
            },
            "leader": None,
        }
        if comparable:
            scores = {
                vid: c.score for vid, c in cells.items() if c is not None and c.score is not None
            }
            best = min(scores.values())
            leaders = [vid for vid, value in scores.items() if value == best]
            row["leader"] = str(leaders[0]) if len(leaders) == 1 else None
        else:
            row["reason"] = "; ".join(reasons)
        dimensions.append(row)

    signals = _compare_signals(ordered, names)
    comparable_dimensions = sum(1 for d in dimensions if d["comparable"])
    ranking, suppressed = _rank(config, ordered, comparable_dimensions)
    return {
        "scoring_config_version": config.version,
        "vendors": [
            {
                "id": str(s.vendor_id),
                "name": s.vendor_name,
                "assessment_id": str(s.assessment_id),
                "overall_score": s.overall_score,
                "provisional": s.provisional,
            }
            for s in ordered
        ],
        "ranking": ranking,
        "ranking_suppressed_reason": suppressed,
        "dimensions": dimensions,
        "signals": signals,
        "vendor_order": ids,
    }


def _rank(
    config: ScoringConfig, snapshots: Sequence[AssessmentSnapshot], comparable_dimensions: int
) -> tuple[list[str], str | None]:
    provisional = [s.vendor_name for s in snapshots if s.provisional]
    if provisional:
        return [], "provisional assessment: " + ", ".join(provisional)
    if any(s.overall_score is None for s in snapshots):
        return [], "a vendor has no overall score"
    need = config.comparison.min_comparable_dimensions_to_rank
    if comparable_dimensions < need:
        return (
            [],
            f"only {comparable_dimensions} dimensions are comparable (at least {need} needed)",
        )
    by_risk = sorted(snapshots, key=lambda s: (s.overall_score or 0.0, str(s.vendor_id)))
    return [str(s.vendor_id) for s in by_risk], None


def _signal_cell(signal: SignalSnapshot | None) -> dict[str, Any]:
    if signal is None:
        return {"status": "insufficient_evidence"}
    cell: dict[str, Any] = {"status": signal.status}
    if signal.status == "absent" and signal.policy_treated:
        cell["label"] = POLICY_LABEL
    return cell


def _compare_signals(
    snapshots: Sequence[AssessmentSnapshot], names: dict[UUID, str]
) -> list[dict[str, Any]]:
    by_vendor = {s.vendor_id: {sig.signal_id: sig for sig in s.signals} for s in snapshots}
    signal_ids = sorted({sig_id for signals in by_vendor.values() for sig_id in signals})
    rows: list[dict[str, Any]] = []
    for signal_id in signal_ids:
        found = {vid: signals.get(signal_id) for vid, signals in by_vendor.items()}
        first = next(s for s in found.values() if s is not None)
        cells = {str(vid): _signal_cell(sig) for vid, sig in found.items()}
        usable = {
            vid: sig is not None
            and (sig.status in EVIDENCE_BASED or (sig.status == "absent" and sig.policy_treated))
            for vid, sig in found.items()
        }
        all_absent_alike = all(
            sig is not None
            and sig.status == "absent"
            and sig.policy_treated == first.policy_treated
            for sig in found.values()
        )
        comparable = all(usable.values()) or all_absent_alike
        row: dict[str, Any] = {
            "signal_id": signal_id,
            "dimension": first.dimension.value,
            "label": first.label,
            "comparable": comparable,
            "cells": cells,
        }
        if not comparable:
            missing = [names[vid] for vid, ok in usable.items() if not ok]
            row["reason"] = "Missing in " + ", ".join(missing) + " — cannot compare"
        rows.append(row)
    return rows


# ---- narrative ------------------------------------------------------------------------------


def template_summary(result: dict[str, Any]) -> str:
    """The deterministic summary used when no narrative passes validation."""
    vendors = {v["id"]: v for v in result["vendors"]}
    lines = [
        "Comparison of "
        + ", ".join(v["name"] for v in result["vendors"])
        + f" (scoring {result['scoring_config_version']})."
    ]
    if result["ranking"]:
        order = " < ".join(
            f"{vendors[i]['name']} ({vendors[i]['overall_score']:.1f})" for i in result["ranking"]
        )
        lines.append(f"Overall risk, lowest first: {order}.")
    else:
        lines.append(f"No overall ranking: {result['ranking_suppressed_reason']}.")
    for row in result["dimensions"]:
        label = row["dimension"]
        if row["comparable"]:
            leader = vendors[row["leader"]]["name"] if row["leader"] else "no single vendor"
            lines.append(f"{label}: comparable; lowest risk: {leader}.")
        else:
            lines.append(f"{label}: not comparable ({row['reason']}).")
    gaps = sum(1 for s in result["signals"] if not s["comparable"])
    if gaps:
        lines.append(f"{gaps} signals cannot be compared because a vendor lacks evidence for them.")
    return "\n".join(lines)


def validate_narrative(text: str, result: dict[str, Any], known_signals: set[str]) -> list[str]:
    """Reasons the narrative must not be used. Empty means it may be."""
    problems: list[str] = []
    words = len(text.split())
    if not WORDS_MIN <= words <= WORDS_MAX:
        problems.append(f"length {words} words, expected {WORDS_MIN} to {WORDS_MAX}")

    dimension_names = {d.value for d in RiskDimension}
    for token in _SIGNAL_ID.findall(text):
        if token not in known_signals and token.lower().replace("-", "_") not in dimension_names:
            problems.append(f"unknown signal id {token}")

    not_comparable_dims = [r["dimension"] for r in result["dimensions"] if not r["comparable"]]
    not_comparable_signals = [s["signal_id"] for s in result["signals"] if not s["comparable"]]
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if not _COMPARATIVE.search(sentence):
            continue
        lowered = sentence.lower()
        for dimension in not_comparable_dims:
            if dimension in lowered or dimension.replace("_", " ") in lowered:
                problems.append(f"compares {dimension}, which is not comparable")
        for signal_id in not_comparable_signals:
            if signal_id in sentence:
                problems.append(f"compares {signal_id}, which is not comparable")
    if not result["ranking"] and re.search(r"\b(overall|ranked|ranking)\b", text, re.I):
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            if re.search(r"\b(overall|rank\w*)\b", sentence, re.I) and _COMPARATIVE.search(
                sentence
            ):
                if not re.search(r"\b(no|not|cannot|suppressed|without)\b", sentence, re.I):
                    problems.append("ranks the vendors although no ranking was produced")
    return sorted(set(problems))
