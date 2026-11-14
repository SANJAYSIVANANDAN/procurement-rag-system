"""The vendor taxonomy and scoring configuration: the shipped files, and refusing bad ones."""

from pathlib import Path

import pytest
import yaml

from covenantiq.analysis.contract.rules_loader import get_ruleset
from covenantiq.analysis.vendor.config_schema import (
    VendorConfigError,
    get_vendor_config,
    load_vendor_config,
)
from covenantiq.core.enums import RiskDimension, Severity

SIGNALS = Path("config/vendor/signals_v1.yaml")
SCORING = Path("config/vendor/scoring_v1.yaml")


class TestShippedConfig:
    def test_it_loads_and_matches_the_contract_ruleset(self) -> None:
        config = get_vendor_config()
        assert config.scoring.version == "vendor_scoring_v1@1.0.0"
        assert config.signals.version == "vendor_signals_v1@1.0.0"
        rule_ids = {r.id for r in get_ruleset().file.rules}
        for signal in config.signals.signals:
            assert set(signal.source.contract_rules) <= rule_ids

    def test_the_taxonomy_is_the_one_in_the_spec(self) -> None:
        signals = get_vendor_config().signals
        assert len(signals.signals) == 26
        by_dimension = {d: len(signals.for_dimension(d)) for d in RiskDimension}
        assert by_dimension == {
            RiskDimension.LEGAL: 4,
            RiskDimension.DATA_SECURITY: 5,
            RiskDimension.OPERATIONAL: 5,
            RiskDimension.FINANCIAL_COMMERCIAL: 5,
            RiskDimension.COMPLIANCE: 3,
            RiskDimension.DEPENDENCY: 4,
        }
        ledger = signals.by_id()
        assert ledger["LEG-INDEMNITY"].source.contract_rules == ["IND-001", "IND-002"]
        assert ledger["LEG-LIAB-CAP"].weight == 3 and ledger["DS-PEN-TEST"].weight == 1
        assert not ledger["DS-PEN-TEST"].from_contract and ledger["DS-PEN-TEST"].source.extraction

    def test_the_weights_are_the_specs_and_sum_to_one(self) -> None:
        weights = get_vendor_config().scoring.dimension_weights
        assert weights[RiskDimension.LEGAL] == 0.25 and weights[RiskDimension.DEPENDENCY] == 0.05
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_the_three_documented_absences_count_as_risk(self) -> None:
        overrides = get_vendor_config().scoring.absent_policy.overrides
        assert set(overrides) == {"LEG-LIAB-CAP", "DS-BREACH-NOTICE", "DEP-EXIT"}
        assert overrides["DEP-EXIT"].severity == "medium"

    @pytest.mark.parametrize(
        ("score", "severity"),
        [
            (None, Severity.INFO),
            (0, Severity.LOW),
            (24.99, Severity.LOW),
            (25, Severity.MEDIUM),
            (49.99, Severity.MEDIUM),
            (50, Severity.HIGH),
            (74.99, Severity.HIGH),
            (75, Severity.CRITICAL),
            (100, Severity.CRITICAL),
        ],
    )
    def test_severity_bands_have_inclusive_lower_edges(
        self, score: float | None, severity: Severity
    ) -> None:
        assert get_vendor_config().scoring.band(score) is severity


class TestRefusingBadFiles:
    def edit(self, tmp_path: Path, path: Path, mutate) -> Path:  # type: ignore[no-untyped-def]
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutate(raw)
        out = tmp_path / path.name
        out.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return out

    def load(self, signals: Path = SIGNALS, scoring: Path = SCORING):  # type: ignore[no-untyped-def]
        return load_vendor_config(
            signals, scoring, contract_rule_ids={r.id for r in get_ruleset().file.rules}
        )

    def test_weights_that_do_not_sum_to_one(self, tmp_path: Path) -> None:
        bad = self.edit(tmp_path, SCORING, lambda r: r["dimension_weights"].update(legal=0.30))
        with pytest.raises(VendorConfigError, match=r"sum to 1\.0"):
            self.load(scoring=bad)

    def test_a_missing_dimension_weight(self, tmp_path: Path) -> None:
        bad = self.edit(tmp_path, SCORING, lambda r: r["dimension_weights"].pop("dependency"))
        with pytest.raises(VendorConfigError, match="all six"):
            self.load(scoring=bad)

    def test_an_absent_override_for_a_signal_that_does_not_exist(self, tmp_path: Path) -> None:
        bad = self.edit(
            tmp_path,
            SCORING,
            lambda r: r["absent_policy"]["overrides"].update(
                NOPE={"treat_as": "unfavorable", "severity": "high", "rationale": "x"}
            ),
        )
        with pytest.raises(VendorConfigError, match="unknown signal: NOPE"):
            self.load(scoring=bad)

    def test_a_duplicate_signal_id(self, tmp_path: Path) -> None:
        bad = self.edit(tmp_path, SIGNALS, lambda r: r["signals"].append(dict(r["signals"][0])))
        with pytest.raises(VendorConfigError, match="duplicate signal"):
            self.load(signals=bad)

    def test_a_dimension_with_no_signals(self, tmp_path: Path) -> None:
        bad = self.edit(
            tmp_path,
            SIGNALS,
            lambda r: r.update(signals=[s for s in r["signals"] if s["dimension"] != "dependency"]),
        )
        with pytest.raises(VendorConfigError, match="dependency"):
            self.load(signals=bad)

    def test_a_signal_that_names_a_contract_rule_that_does_not_exist(self, tmp_path: Path) -> None:
        bad = self.edit(
            tmp_path,
            SIGNALS,
            lambda r: r["signals"][0]["source"].update(contract_rules=["ZZZ-999"]),
        )
        with pytest.raises(VendorConfigError, match="LEG-LIAB-CAP: unknown contract rule ZZZ-999"):
            self.load(signals=bad)

    def test_a_signal_with_both_sources_or_neither(self, tmp_path: Path) -> None:
        both = self.edit(
            tmp_path,
            SIGNALS,
            lambda r: r["signals"][0]["source"].update(
                extraction={"queries": ["q"], "question": "q?"}
            ),
        )
        with pytest.raises(VendorConfigError, match="either"):
            self.load(signals=both)

    def test_a_favorable_status_riskier_than_another_is_refused(self, tmp_path: Path) -> None:
        bad = self.edit(tmp_path, SCORING, lambda r: r["signal_risk"].update(favorable=60))
        with pytest.raises(VendorConfigError, match="favorable"):
            self.load(scoring=bad)

    def test_unfavorable_risk_must_not_fall_with_severity(self, tmp_path: Path) -> None:
        bad = self.edit(
            tmp_path, SCORING, lambda r: r["signal_risk"]["unfavorable"].update(critical=40)
        )
        with pytest.raises(VendorConfigError, match="severity"):
            self.load(scoring=bad)

    def test_bands_must_increase(self, tmp_path: Path) -> None:
        bad = self.edit(tmp_path, SCORING, lambda r: r["severity_bands"].update(low=60))
        with pytest.raises(VendorConfigError, match="increase"):
            self.load(scoring=bad)

    def test_unknown_fields_and_a_missing_file(self, tmp_path: Path) -> None:
        bad = self.edit(tmp_path, SCORING, lambda r: r.update(surprise=1))
        with pytest.raises(VendorConfigError):
            self.load(scoring=bad)
        with pytest.raises(VendorConfigError, match="cannot read"):
            self.load(scoring=tmp_path / "nope.yaml")
