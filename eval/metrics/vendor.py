"""Metrics on vendor assessment (spec 28.3): determinism, signal agreement, dimension order."""

from collections.abc import Callable, Mapping
from typing import Any

from eval.metrics.stats import Ratio


def signal_agreement(expected: Mapping[str, str], actual: Mapping[str, str]) -> Ratio:
    """The share of labelled signals whose aggregate status is as labelled."""
    agreed = sum(1 for signal, status in expected.items() if actual.get(signal) == status)
    return float(agreed), float(len(expected))


def dimension_order(
    claims: Mapping[str, str],
    vendor: str,
    scores: Mapping[str, Mapping[str, float | None]],
) -> Ratio:
    """Claims such as `higher_risk_than:Acme` for a dimension: true if this vendor's score is
    higher than the named vendor's. A score that does not exist makes the claim false."""
    held = 0
    for dimension, claim in claims.items():
        relation, _, other = claim.partition(":")
        mine = scores.get(vendor, {}).get(dimension)
        theirs = scores.get(other, {}).get(dimension)
        if relation == "higher_risk_than" and mine is not None and theirs is not None:
            held += mine > theirs
    return float(held), float(len(claims))


def determinism(run: Callable[[], Any], times: int = 3) -> Ratio:
    """Run the scoring `times` times on identical input; 1 if every output is identical."""
    first = run()
    same = all(run() == first for _ in range(times - 1))
    return (1.0 if same else 0.0), 1.0
