"""Aggregating per-example results into a metric value with a bootstrap confidence interval.

Every metric here is a ratio over examples: each example contributes a `(numerator, denominator)`
pair, the metric is `sum(numerators) / sum(denominators)`, and the interval comes from resampling
the examples with replacement (a fixed seed, so the same results give the same interval).
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass

Ratio = tuple[float, float]  # (numerator, denominator) for one example
BOOTSTRAP_ITERATIONS = 1000
BOOTSTRAP_SEED = 20261114


@dataclass(frozen=True)
class MetricValue:
    name: str
    value: float | None  # None when there was nothing to measure
    n: int  # examples that contributed
    ci_low: float | None
    ci_high: float | None
    higher_is_better: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": None if self.value is None else round(self.value, 6),
            "n": self.n,
            "ci95": [
                None if self.ci_low is None else round(self.ci_low, 6),
                None if self.ci_high is None else round(self.ci_high, 6),
            ],
            "higher_is_better": self.higher_is_better,
        }


def ratio_of(pairs: Sequence[Ratio]) -> float | None:
    denominator = sum(d for _, d in pairs)
    return sum(n for n, _ in pairs) / denominator if denominator else None


def bootstrap_interval(
    pairs: Sequence[Ratio],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float | None, float | None]:
    """The 2.5th and 97.5th percentiles of the ratio over resampled examples."""
    if not pairs:
        return None, None
    rng = random.Random(seed)  # noqa: S311  (resampling, not security)
    size = len(pairs)
    estimates: list[float] = []
    for _ in range(iterations):
        value = ratio_of([pairs[rng.randrange(size)] for _ in range(size)])
        if value is not None:
            estimates.append(value)
    if not estimates:
        return None, None
    estimates.sort()
    return estimates[int(0.025 * (len(estimates) - 1))], estimates[
        int(0.975 * (len(estimates) - 1))
    ]


def summarise(name: str, pairs: Sequence[Ratio], *, higher_is_better: bool = True) -> MetricValue:
    """Examples with a zero denominator say nothing and are not counted in `n`."""
    counted = [p for p in pairs if p[1] > 0]
    low, high = bootstrap_interval(counted)
    return MetricValue(name, ratio_of(counted), len(counted), low, high, higher_is_better)
