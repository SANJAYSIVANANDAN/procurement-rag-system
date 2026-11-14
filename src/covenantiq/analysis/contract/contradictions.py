"""Internal contradictions (spec 20.6): two clauses of one document that disagree.

Candidates are found deterministically: each rule can name a value to look for (a notice period, a
payment term, the governing law), and two different values for the same key in different passages
make a pair. A model then confirms or dismisses each pair (at most a handful per document), because
two different numbers are often two different things ("30 days to pay" and "5 days to dispute").
Only confirmed pairs become findings.
"""

import re
from collections.abc import Sequence
from itertools import combinations

from covenantiq.analysis.contract.rules_schema import Compare, RuleSpec
from covenantiq.analysis.contract.types import SourceRef, ValueObservation

_NOISE = re.compile(r"\b(the|state|commonwealth|of|laws?|republic)\b")


def normalise(value: str, unit: str) -> str | None:
    """A comparable form of a captured value, or None if it is not usable."""
    text = value.strip()
    if unit in ("days", "hours"):
        digits = re.sub(r"\D", "", text)
        return digits.lstrip("0") or None
    cleaned = " ".join(_NOISE.sub(" ", text.lower()).replace("&", "and").split())
    return cleaned or None


def observe(rule: RuleSpec, sources: Sequence[SourceRef]) -> list[ValueObservation]:
    """The values this rule's `compare` specs find in its sources, once per (key, value, source)."""
    found: list[ValueObservation] = []
    for spec in rule.compare:
        found.extend(_observe_spec(rule.id, spec, sources))
    return found


def _observe_spec(
    rule_id: str, spec: Compare, sources: Sequence[SourceRef]
) -> list[ValueObservation]:
    pattern = re.compile(spec.pattern)
    out: list[ValueObservation] = []
    for source in sources:
        seen: set[str] = set()
        for match in pattern.finditer(source.text):
            raw = match.group(1)
            value = normalise(raw, spec.unit)
            if value is None or value in seen:
                continue
            seen.add(value)
            out.append(
                ValueObservation(
                    key=spec.key,
                    value=value,
                    raw=" ".join(raw.split()),
                    unit=spec.unit,
                    rule_id=rule_id,
                    source=source,
                )
            )
    return out


def find_candidates(
    observations: Sequence[ValueObservation], *, limit: int
) -> list[tuple[ValueObservation, ValueObservation]]:
    """Pairs that disagree: the same key, different values, in different passages. Deterministic
    order (by key, then first seen); at most `limit`, spread across keys so one noisy key cannot
    use them all."""
    by_key: dict[str, list[ValueObservation]] = {}
    for observation in observations:
        by_key.setdefault(observation.key, []).append(observation)

    per_key: list[list[tuple[ValueObservation, ValueObservation]]] = []
    for key in sorted(by_key):
        pairs = [
            (a, b)
            for a, b in combinations(by_key[key], 2)
            if a.value != b.value and _different_passages(a, b)
        ]
        if pairs:
            per_key.append(pairs)

    chosen: list[tuple[ValueObservation, ValueObservation]] = []
    depth = 0
    while len(chosen) < limit and any(len(p) > depth for p in per_key):
        for pairs in per_key:
            if len(pairs) > depth and len(chosen) < limit:
                chosen.append(pairs[depth])
        depth += 1
    return chosen


def _different_passages(a: ValueObservation, b: ValueObservation) -> bool:
    """The same chunk can be a candidate for two rules, so compare what the passage is, not which
    rule found it."""
    return set(map(str, a.source.chunk_ids)) != set(map(str, b.source.chunk_ids)) or not (
        a.source.chunk_ids or b.source.chunk_ids
    )
