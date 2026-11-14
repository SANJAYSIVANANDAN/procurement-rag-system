"""Deterministic red flags: regular expressions over the passages retrieved for a rule (spec 20.4).

A heuristic can force a status (only ever towards risk), raise a severity, or add a flag. It never
makes a clause acceptable: absence of a red flag is not evidence of a good clause.
"""

import re
from collections.abc import Sequence
from functools import lru_cache

from covenantiq.analysis.contract.rules_schema import RuleSpec
from covenantiq.analysis.contract.types import HeuristicHit, SourceRef

SPAN_CHARS = 200
MAX_SCAN_CHARS = 8000  # one passage; bounds the work a pattern can do


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _span(text: str, match: re.Match[str]) -> str:
    start = max(0, match.start() - 40)
    end = min(len(text), match.end() + 40)
    snippet = " ".join(text[start:end].split())
    return snippet if len(snippet) <= SPAN_CHARS else snippet[:SPAN_CHARS] + "…"


def run_heuristics(rule: RuleSpec, sources: Sequence[SourceRef]) -> list[HeuristicHit]:
    """Every heuristic of `rule` against every source, at most one hit per (heuristic, source)."""
    hits: list[HeuristicHit] = []
    for heuristic in rule.heuristics:
        pattern = _compiled(heuristic.pattern)
        for source in sources:
            match = pattern.search(source.text[:MAX_SCAN_CHARS])
            if match is None:
                continue
            hits.append(
                HeuristicHit(
                    rule_id=rule.id,
                    heuristic_id=heuristic.id,
                    marker=source.marker,
                    chunk_id=source.chunk_ids[0] if source.chunk_ids else None,
                    span=_span(source.text, match),
                    status=heuristic.effect.status,
                    severity=heuristic.effect.severity,
                    flag=heuristic.effect.flag,
                )
            )
    return hits


def forced_problem(hits: Sequence[HeuristicHit]) -> list[HeuristicHit]:
    return [hit for hit in hits if hit.status == "problematic"]
