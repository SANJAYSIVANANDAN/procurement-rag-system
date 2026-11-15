"""Metrics on contract analysis findings (spec 28.3)."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from eval.dataset import ExpectedFinding
from eval.metrics.stats import Ratio
from eval.metrics.text import OVERLAP_THRESHOLD, overlaps

PRESENT = {"acceptable", "problematic", "ambiguous"}
RISKY = {"problematic", "ambiguous"}


@dataclass(frozen=True)
class ProducedFinding:
    rule_id: str
    status: str
    evidence_texts: Sequence[str] = field(default_factory=tuple)


def _by_rule(produced: Sequence[ProducedFinding]) -> dict[str, list[ProducedFinding]]:
    index: dict[str, list[ProducedFinding]] = {}
    for finding in produced:
        index.setdefault(finding.rule_id, []).append(finding)
    return index


def clause_extraction_accuracy(
    expected: Sequence[ExpectedFinding], produced: Sequence[ProducedFinding]
) -> Ratio:
    """For each expected clause: is it reported as present or absent as labelled, and, if present,
    does the evidence overlap the labelled passage? `insufficient_evidence` counts as a miss."""
    index = _by_rule(produced)
    correct = 0
    for item in expected:
        found = index.get(item.rule_id, [])
        if not found:
            continue
        got = found[0]
        if item.status == "absent":
            correct += got.status == "absent"
        elif item.status in PRESENT and got.status in PRESENT:
            wanted = [p.text for p in item.evidence]
            correct += not wanted or any(
                overlaps(text, w, OVERLAP_THRESHOLD) for text in got.evidence_texts for w in wanted
            )
    return float(correct), float(len(expected))


def risk_precision(
    expected: Sequence[ExpectedFinding], produced: Sequence[ProducedFinding]
) -> Ratio:
    """Of the problematic or ambiguous findings produced for labelled rules: the share that match
    a labelled finding (same rule and status)."""
    labelled = {(e.rule_id, e.status) for e in expected}
    rules = {e.rule_id for e in expected}
    risky = [f for f in produced if f.status in RISKY and f.rule_id in rules]
    return float(sum((f.rule_id, f.status) in labelled for f in risky)), float(len(risky))


def risk_recall(expected: Sequence[ExpectedFinding], produced: Sequence[ProducedFinding]) -> Ratio:
    """Of the labelled problematic or ambiguous findings: the share that were produced."""
    made = {(f.rule_id, f.status) for f in produced}
    wanted = [e for e in expected if e.status in RISKY]
    return float(sum((e.rule_id, e.status) in made for e in wanted)), float(len(wanted))


def status_accuracy(
    expected: Sequence[ExpectedFinding], produced: Sequence[ProducedFinding]
) -> Ratio:
    """The exact status of every labelled rule (extra to the spec: it is the plainest summary)."""
    index = _by_rule(produced)
    correct = sum(
        1 for e in expected if index.get(e.rule_id) and index[e.rule_id][0].status == e.status
    )
    return float(correct), float(len(expected))


def not_expected_violations(
    not_expected: Sequence[str], produced: Sequence[ProducedFinding]
) -> Ratio:
    """How many of the `RULE:status` pairs that must never appear did appear. Lower is better."""
    made = {f"{f.rule_id}:{f.status}" for f in produced}
    return float(sum(item in made for item in not_expected)), float(len(not_expected))
