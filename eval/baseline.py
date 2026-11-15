"""The regression gate: compare an evaluation report with the committed baseline (spec 28.6, 28.8).

A baseline is a report reduced to its metric values. Updating one is a deliberate act that needs a
justification in the commit message; this module only reads it and judges a new report against it.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

EPSILON = 1e-9
DEFAULT_TOLERANCES = Path("config/eval_tolerances.yaml")


@dataclass(frozen=True)
class Tolerances:
    default_abs: float = 0.02
    per_metric: dict[str, float] = field(default_factory=dict)

    def allowed(self, name: str) -> float:
        return self.per_metric.get(name, self.default_abs)


def load_tolerances(path: Path = DEFAULT_TOLERANCES) -> Tolerances:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Tolerances(
        float(raw.get("default_abs", 0.02)),
        {k: float(v) for k, v in (raw.get("metrics") or {}).items()},
    )


def baseline_from_report(report: dict[str, Any], note: str = "") -> dict[str, Any]:
    return {
        "run": report["run"],
        "dataset": report["dataset"],
        "created": report["created"],
        "git_sha": report["environment"]["git_sha"],
        "note": note,
        "metrics": {
            name: {"value": m["value"], "n": m["n"], "higher_is_better": m["higher_is_better"]}
            for name, m in sorted(report["metrics"].items())
        },
    }


def write_baseline(report: dict[str, Any], path: Path, note: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(baseline_from_report(report, note), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class Row:
    name: str
    baseline: float | None
    current: float | None
    allowed: float
    status: str  # ok, improved, regressed, missing, new

    @property
    def delta(self) -> float | None:
        if self.baseline is None or self.current is None:
            return None
        return self.current - self.baseline


@dataclass
class Comparison:
    rows: list[Row]
    problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems and all(
            r.status != "regressed" and r.status != "missing" for r in self.rows
        )

    def render(self) -> str:
        lines = [
            "metric | baseline | current | change | allowed | status",
            "---|---|---|---|---|---",
        ]
        for r in self.rows:

            def fmt(v: float | None) -> str:
                return "n/a" if v is None else f"{v:.3f}"

            change = "n/a" if r.delta is None else f"{r.delta:+.3f}"
            lines.append(
                f"{r.name} | {fmt(r.baseline)} | {fmt(r.current)} | {change} | "
                f"{r.allowed:.3f} | {r.status}"
            )
        lines += [f"PROBLEM: {p}" for p in self.problems]
        lines.append("RESULT: " + ("pass" if self.passed else "FAIL"))
        return "\n".join(lines)


def compare(baseline: dict[str, Any], report: dict[str, Any], tolerances: Tolerances) -> Comparison:
    problems: list[str] = []
    if baseline["dataset"]["version"] != report["dataset"]["version"]:
        problems.append(
            f"dataset version {report['dataset']['version']} differs from the baseline's "
            f"{baseline['dataset']['version']}: a baseline is only comparable on its own dataset"
        )
    elif baseline["dataset"]["manifest_sha256"] != report["dataset"]["manifest_sha256"]:
        problems.append(
            "the dataset files changed since the baseline was made (manifest hash differs)"
        )
    if report.get("errors"):
        problems.append(
            f"the run had {len(report['errors'])} error(s): " + "; ".join(report["errors"][:3])
        )

    rows: list[Row] = []
    for name, base in sorted(baseline["metrics"].items()):
        allowed = tolerances.allowed(name)
        current = report["metrics"].get(name, {}).get("value")
        base_value = base["value"]
        if base_value is None:
            rows.append(Row(name, None, current, allowed, "ok"))
            continue
        if current is None:
            rows.append(Row(name, base_value, None, allowed, "missing"))
            continue
        higher = base["higher_is_better"]
        worse_by = (base_value - current) if higher else (current - base_value)
        if worse_by > allowed + EPSILON:
            status = "regressed"
        elif worse_by < -EPSILON:
            status = "improved"
        else:
            status = "ok"
        rows.append(Row(name, base_value, current, allowed, status))
    for name in sorted(set(report["metrics"]) - set(baseline["metrics"])):
        rows.append(
            Row(name, None, report["metrics"][name]["value"], tolerances.allowed(name), "new")
        )
    return Comparison(rows, problems)
