"""Fail if an evaluation report is worse than the committed baseline beyond tolerance.

    uv run python -m eval.scripts.compare_to_baseline [--report PATH] [--baseline PATH]

With no `--report` it takes the newest offline report in `eval/reports`. Exit code 1 on regression.
"""

import argparse
import json
import sys
from pathlib import Path

from eval.baseline import DEFAULT_TOLERANCES, compare, load_tolerances

DEFAULT_BASELINE = Path("eval/baselines/offline_v1.0.0.json")


def newest_report(directory: Path, kind: str = "offline") -> Path:
    candidates = sorted(directory.glob(f"*_{kind}.json"))
    if not candidates:
        raise SystemExit(f"no {kind} report in {directory}: run the evaluation first")
    return candidates[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument("--tolerance", default=str(DEFAULT_TOLERANCES))
    args = parser.parse_args()

    report_path = Path(args.report) if args.report else newest_report(Path("eval/reports"))
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    result = compare(baseline, report, load_tolerances(Path(args.tolerance)))
    print(f"report: {report_path}\nbaseline: {args.baseline}\n")
    print(result.render())
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
