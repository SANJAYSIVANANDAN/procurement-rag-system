"""The evaluation report: what was run, on what, with what, and every number with its interval.

A report records the git revision, the dataset version and manifest hash, the hash of every prompt,
the model used per route, and the ruleset and scoring versions (spec 28.6). A baseline is a report
reduced to its metric values.
"""

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.dataset import Dataset
from eval.metrics.stats import MetricValue

PROMPTS_DIR = Path("src/covenantiq/ai/prompts")


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def prompt_hashes() -> dict[str, str]:
    """The hash of every prompt file, so a change of wording shows in the report."""
    return {
        p.stem: hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        for p in sorted(PROMPTS_DIR.glob("*.md"))
    }


def environment(models: dict[str, str]) -> dict[str, Any]:
    from covenantiq.analysis.contract.rules_loader import get_ruleset
    from covenantiq.analysis.vendor.config_schema import get_vendor_config

    return {
        "git_sha": git_sha(),
        "models": models,
        "prompts": prompt_hashes(),
        "ruleset_version": get_ruleset().version,
        "scoring_config_version": get_vendor_config().scoring.version,
        "signals_version": get_vendor_config().signals.version,
    }


def build_report(
    kind: str,
    dataset: Dataset,
    metrics: list[MetricValue],
    per_example: dict[str, list[dict[str, Any]]],
    models: dict[str, str],
    *,
    errors: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    manifest_hash = hashlib.sha256((dataset.root / "manifest.yaml").read_bytes()).hexdigest()
    return {
        "run": kind,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": {"version": dataset.manifest.version, "manifest_sha256": manifest_hash},
        "environment": environment(models),
        "metrics": {m.name: m.to_dict() for m in metrics},
        "per_example": per_example,
        "errors": errors or [],
        "notes": notes or [],
    }


def write_report(report: dict[str, Any], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = directory / f"{stamp}_{report['run']}.json"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path = json_path.with_suffix(".md")
    md_path.write_text(markdown_summary(report), encoding="utf-8")
    return json_path, md_path


def markdown_summary(report: dict[str, Any]) -> str:
    env = report["environment"]
    lines = [
        f"# Evaluation report: {report['run']}",
        "",
        f"- dataset `{report['dataset']['version']}`, git `{env['git_sha'][:10]}`, "
        f"{report['created']}",
        f"- ruleset `{env['ruleset_version']}`, scoring `{env['scoring_config_version']}`",
        "- models: " + ", ".join(f"{k}={v}" for k, v in sorted(env["models"].items())),
        "",
        "| metric | value | 95% CI | n |",
        "|---|---|---|---|",
    ]
    for name, m in sorted(report["metrics"].items()):
        value = "n/a" if m["value"] is None else f"{m['value']:.3f}"
        low, high = m["ci95"]
        interval = "n/a" if low is None else f"{low:.3f} to {high:.3f}"
        arrow = "" if m["higher_is_better"] else " (lower is better)"
        lines.append(f"| {name}{arrow} | {value} | {interval} | {m['n']} |")
    if report["errors"]:
        lines += ["", "## Errors", *[f"- {e}" for e in report["errors"]]]
    if report["notes"]:
        lines += ["", "## Notes", *[f"- {n}" for n in report["notes"]]]
    return "\n".join(lines) + "\n"
