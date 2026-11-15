"""The regression gate's judgement: what counts as worse, what is allowed, and what is refused."""

import json
from pathlib import Path
from typing import Any

import pytest

from eval.baseline import Tolerances, baseline_from_report, compare, load_tolerances, write_baseline

DATASET = {"version": "1.0.0", "manifest_sha256": "abc"}


def report(**values: float | None) -> dict[str, Any]:
    lower_is_better = {"unsupported_claim_rate", "false_answer_rate"}
    return {
        "run": "offline",
        "created": "2026-11-14T00:00:00+00:00",
        "dataset": dict(DATASET),
        "environment": {"git_sha": "deadbeef"},
        "errors": [],
        "metrics": {
            name: {
                "name": name,
                "value": value,
                "n": 10,
                "ci95": [None, None],
                "higher_is_better": name not in lower_is_better,
            }
            for name, value in values.items()
        },
    }


def verdict(base: dict[str, Any], now: dict[str, Any], tolerances: Tolerances | None = None):  # type: ignore[no-untyped-def]
    return compare(baseline_from_report(base), now, tolerances or Tolerances())


class TestHigherIsBetter:
    def test_equal_passes(self) -> None:
        assert verdict(report(hit_at_5=0.9), report(hit_at_5=0.9)).passed

    def test_a_drop_within_tolerance_passes(self) -> None:
        result = verdict(report(hit_at_5=0.90), report(hit_at_5=0.885))
        assert result.passed and result.rows[0].status == "ok"

    def test_a_drop_beyond_tolerance_fails(self) -> None:
        result = verdict(report(hit_at_5=0.90), report(hit_at_5=0.87))
        assert not result.passed and result.rows[0].status == "regressed"

    def test_exactly_the_tolerance_passes(self) -> None:
        assert verdict(report(hit_at_5=0.90), report(hit_at_5=0.88)).passed

    def test_an_improvement_never_fails(self) -> None:
        result = verdict(report(hit_at_5=0.80), report(hit_at_5=0.99))
        assert result.passed and result.rows[0].status == "improved"


class TestLowerIsBetter:
    def test_a_rise_beyond_tolerance_fails(self) -> None:
        assert not verdict(report(false_answer_rate=0.10), report(false_answer_rate=0.13)).passed

    def test_a_fall_is_an_improvement(self) -> None:
        result = verdict(report(false_answer_rate=0.40), report(false_answer_rate=0.10))
        assert result.passed and result.rows[0].status == "improved"

    def test_a_small_rise_is_tolerated(self) -> None:
        assert verdict(
            report(unsupported_claim_rate=0.02), report(unsupported_claim_rate=0.035)
        ).passed


class TestTolerances:
    def test_a_metric_can_have_its_own_tolerance(self) -> None:
        tolerances = Tolerances(0.02, {"hit_at_5": 0.10})
        assert verdict(report(hit_at_5=0.9), report(hit_at_5=0.82), tolerances).passed
        assert not verdict(report(hit_at_5=0.9), report(hit_at_5=0.75), tolerances).passed

    def test_zero_means_it_must_not_get_worse_at_all(self) -> None:
        tolerances = Tolerances(0.02, {"vendor_scoring_determinism": 0.0})
        assert verdict(
            report(vendor_scoring_determinism=1.0),
            report(vendor_scoring_determinism=1.0),
            tolerances,
        ).passed
        assert not verdict(
            report(vendor_scoring_determinism=1.0),
            report(vendor_scoring_determinism=0.99),
            tolerances,
        ).passed

    def test_the_shipped_tolerances_load_and_pin_determinism(self) -> None:
        tolerances = load_tolerances()
        assert (
            tolerances.default_abs == 0.02
            and tolerances.allowed("vendor_scoring_determinism") == 0.0
        )
        assert tolerances.allowed("anything_else") == 0.02


class TestWhatElseFailsTheGate:
    def test_a_metric_that_disappeared_fails(self) -> None:
        result = verdict(report(hit_at_5=0.9, mrr=0.7), report(hit_at_5=0.9))
        assert not result.passed and {r.name: r.status for r in result.rows}["mrr"] == "missing"

    def test_a_metric_that_became_unmeasurable_fails(self) -> None:
        assert not verdict(report(hit_at_5=0.9), report(hit_at_5=None)).passed

    def test_a_new_metric_is_reported_not_judged(self) -> None:
        result = verdict(report(hit_at_5=0.9), report(hit_at_5=0.9, new_thing=0.1))
        assert result.passed and {r.name: r.status for r in result.rows}["new_thing"] == "new"

    def test_a_metric_that_was_never_measured_in_the_baseline_cannot_regress(self) -> None:
        assert verdict(report(hit_at_5=None), report(hit_at_5=0.1)).passed

    def test_a_different_dataset_version_is_refused(self) -> None:
        now = report(hit_at_5=0.9)
        now["dataset"] = {"version": "1.1.0", "manifest_sha256": "abc"}
        result = verdict(report(hit_at_5=0.9), now)
        assert not result.passed and "differs from the baseline" in result.problems[0]

    def test_a_changed_dataset_under_the_same_version_is_refused(self) -> None:
        now = report(hit_at_5=0.9)
        now["dataset"] = {"version": "1.0.0", "manifest_sha256": "different"}
        assert "changed since the baseline" in verdict(report(hit_at_5=0.9), now).problems[0]

    def test_a_run_with_errors_fails_even_if_the_numbers_are_fine(self) -> None:
        now = report(hit_at_5=0.9)
        now["errors"] = ["chat qa-0001: boom"]
        result = verdict(report(hit_at_5=0.9), now)
        assert not result.passed and "1 error" in result.problems[0]


class TestRendering:
    def test_the_table_names_each_metric_and_the_result(self) -> None:
        text = verdict(report(hit_at_5=0.9), report(hit_at_5=0.7)).render()
        assert "hit_at_5 | 0.900 | 0.700 | -0.200 | 0.020 | regressed" in text
        assert text.endswith("RESULT: FAIL")
        assert verdict(report(hit_at_5=0.9), report(hit_at_5=0.9)).render().endswith("RESULT: pass")


class TestBaselineFile:
    def test_a_baseline_keeps_values_not_per_example_detail(self, tmp_path: Path) -> None:
        full = report(hit_at_5=0.9)
        full["per_example"] = {"chat": [{"id": "x"}]}
        path = tmp_path / "b.json"
        write_baseline(full, path, "because")
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["note"] == "because" and saved["metrics"]["hit_at_5"]["value"] == 0.9
        assert "per_example" not in saved and saved["dataset"] == DATASET

    def test_the_shipped_baseline_matches_the_shipped_dataset(self) -> None:
        from eval.dataset import load_dataset
        from eval.report import hashlib

        baseline = json.loads(
            Path("eval/baselines/offline_v1.0.0.json").read_text(encoding="utf-8")
        )
        dataset = load_dataset()
        manifest_hash = hashlib.sha256((dataset.root / "manifest.yaml").read_bytes()).hexdigest()
        assert baseline["dataset"] == {"version": "1.0.0", "manifest_sha256": manifest_hash}
        assert baseline["metrics"]["vendor_scoring_determinism"]["value"] == 1.0
        assert "hit_at_5" in baseline["metrics"] and baseline["note"]


@pytest.mark.parametrize("name", ["hit_at_5", "mrr"])
def test_metric_names_in_the_baseline_are_the_ones_the_runner_reports(name: str) -> None:
    baseline = json.loads(Path("eval/baselines/offline_v1.0.0.json").read_text(encoding="utf-8"))
    assert name in baseline["metrics"]
