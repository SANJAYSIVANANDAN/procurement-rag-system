"""The ruleset: the shipped file is valid, every pattern does what it claims, and bad files are
refused at start-up."""

import re
from pathlib import Path

import pytest
import yaml

from covenantiq.analysis.contract.rules_loader import (
    RulesetError,
    load_ruleset,
    taxonomy_types,
)
from covenantiq.core.enums import ContractType

RULES = Path("config/rules/contract_baseline_v1.yaml")


@pytest.fixture(scope="module")
def ruleset():  # type: ignore[no-untyped-def]
    return load_ruleset(RULES)


class TestShippedRuleset:
    def test_it_has_the_twenty_five_baseline_rules_and_a_version(self, ruleset) -> None:  # type: ignore[no-untyped-def]
        ids = [r.id for r in ruleset.file.rules]
        assert len(ids) == 25 and len(set(ids)) == 25
        assert ruleset.version == "contract_baseline_v1@1.0.0"
        assert {"LIAB-001", "DP-002", "AUD-001", "FM-001"} <= set(ids)

    def test_every_clause_type_exists_in_the_taxonomy(self, ruleset) -> None:  # type: ignore[no-untyped-def]
        known = taxonomy_types()
        for rule in ruleset.file.rules:
            assert set(rule.clause_types) <= known, rule.id

    def test_every_rule_has_queries_questions_and_actions_for_each_risky_status(
        self, ruleset
    ) -> None:  # type: ignore[no-untyped-def]
        for rule in ruleset.file.rules:
            assert rule.retrieval.queries and rule.assessment_questions, rule.id
            assert set(rule.recommended_actions) == {"absent", "problematic", "ambiguous"}, rule.id

    def test_the_spec_default_severities_when_absent(self, ruleset) -> None:  # type: ignore[no-untyped-def]
        expected = {
            "LIAB-001": "high",
            "LIAB-002": "medium",
            "IND-002": "high",
            "TERM-003": "high",
            "REN-001": "info",
            "PAY-002": "info",
            "IP-001": "high",
            "IP-002": "high",
            "CONF-001": "high",
            "DP-001": "high",
            "DP-002": "high",
            "SLA-002": "low",
            "GOV-001": "low",
            "AUD-001": "low",
            "FM-001": "low",
        }
        for rule_id, severity in expected.items():
            assert ruleset.rule(rule_id).severity_policy.absent.value == severity, rule_id

    def test_a_heuristic_can_only_push_towards_risk(self, ruleset) -> None:  # type: ignore[no-untyped-def]
        for rule in ruleset.file.rules:
            for heuristic in rule.heuristics:
                assert heuristic.effect.status in (None, "problematic"), rule.id

    def test_unusual_provisions_have_their_own_rule_ids(self, ruleset) -> None:  # type: ignore[no-untyped-def]
        assert {u.rule_id for u in ruleset.unusual} == {
            "UNUSUAL-EXCLUSIVITY",
            "UNUSUAL-UNILATERAL_AMENDMENT",
            "UNUSUAL-NON_SOLICIT",
            "UNUSUAL-LIQUIDATED_DAMAGES",
            "UNUSUAL-MOST_FAVOURED_CUSTOMER",
        }


class TestApplicability:
    def ids(self, ruleset, kind: ContractType) -> set[str]:  # type: ignore[no-untyped-def]
        return {r.id for r in ruleset.applicable(kind)}

    def test_an_nda_gets_only_the_rules_for_everything_that_are_not_excluded(self, ruleset) -> None:  # type: ignore[no-untyped-def]
        nda = self.ids(ruleset, ContractType.NDA)
        assert {"LIAB-001", "TERM-002", "CONF-001", "GOV-001"} <= nda
        assert not nda & {"PAY-001", "PAY-003", "FM-001", "SLA-001", "DP-001"}

    def test_a_saas_agreement_gets_the_service_rules(self, ruleset) -> None:  # type: ignore[no-untyped-def]
        saas = self.ids(ruleset, ContractType.SAAS)
        assert {"SLA-001", "SLA-002", "DP-001", "DP-003", "TERM-003", "PAY-001"} <= saas
        assert "IP-001" not in saas  # services, sow and msa only

    def test_an_unknown_type_gets_the_all_rules_plus_the_msa_rules(self, ruleset) -> None:  # type: ignore[no-untyped-def]
        unknown = self.ids(ruleset, ContractType.UNKNOWN)
        assert unknown == self.ids(ruleset, ContractType.MSA)
        assert "REN-001" in unknown and "SLA-001" not in unknown


class TestPatterns:
    """Each pattern is tried on text it must catch and text it must not."""

    def hit(self, ruleset, rule_id: str, heuristic: str, text: str) -> bool:  # type: ignore[no-untyped-def]
        rule = ruleset.rule(rule_id)
        pattern = next(h.pattern for h in rule.heuristics if h.id == heuristic)
        return re.search(pattern, text) is not None

    @pytest.mark.parametrize(
        ("rule_id", "heuristic", "text", "expected"),
        [
            (
                "LIAB-001",
                "UNLIMITED",
                "The Supplier accepts unlimited liability for data loss.",
                True,
            ),
            ("LIAB-001", "UNLIMITED", "There is no limitation of liability for fraud.", True),
            ("LIAB-001", "UNLIMITED", "Liability is limited to the fees paid.", False),
            (
                "LIAB-001",
                "CARVE_OUT_DATA",
                "The cap shall not apply to breaches of data protection.",
                True,
            ),
            ("PAY-002", "UNILATERAL_PRICE", "Vendor may increase the fees at any time.", True),
            ("PAY-002", "UNILATERAL_PRICE", "Fees are fixed for the initial term.", False),
            ("REN-001", "AUTO_RENEW", "This Agreement automatically renews for one year.", True),
            ("SLA-001", "LOW_UPTIME", "The service will achieve 95% availability.", True),
            ("SLA-001", "LOW_UPTIME", "The service will achieve 99.9% availability.", False),
            ("PAY-003", "HIGH_INTEREST", "Interest accrues at 18% per annum.", True),
            ("PAY-003", "HIGH_INTEREST", "Interest accrues at 4% per annum.", False),
            (
                "IP-001",
                "VENDOR_OWNS",
                "Vendor retains all right, title and interest in the deliverables.",
                True,
            ),
        ],
    )
    def test_heuristics(
        self, ruleset, rule_id: str, heuristic: str, text: str, expected: bool
    ) -> None:  # type: ignore[no-untyped-def]
        assert self.hit(ruleset, rule_id, heuristic, text) is expected

    @pytest.mark.parametrize(
        ("rule_id", "text", "value"),
        [
            ("PAY-001", "Invoices are payable within 30 days of receipt.", "30"),
            (
                "TERM-002",
                "If the breach is not remedied within 45 days, either party may terminate.",
                "45",
            ),
            (
                "DP-002",
                "The Processor shall notify the Controller of a breach within 48 hours.",
                "48",
            ),
            (
                "GOV-001",
                "This Agreement is governed by the laws of England and Wales.",
                "England and Wales",
            ),
        ],
    )
    def test_compare_patterns_capture_the_value(
        self, ruleset, rule_id: str, text: str, value: str
    ) -> None:  # type: ignore[no-untyped-def]
        spec = ruleset.rule(rule_id).compare[0]
        match = re.search(spec.pattern, text)
        assert match is not None and match.group(1).strip() == value

    @pytest.mark.parametrize(
        "text",
        [
            "The Supplier is the exclusive supplier of the Services.",
            "Either party may amend these terms at any time upon notice.",
            "Neither party shall solicit or hire the other's employees.",
            "The parties agree to liquidated damages of 5% per week.",
            "Customer receives most favoured customer pricing.",
        ],
    )
    def test_unusual_provisions_match(self, ruleset, text: str) -> None:  # type: ignore[no-untyped-def]
        assert any(re.search(u.pattern, text) for u in ruleset.unusual)

    def test_ordinary_text_matches_no_unusual_provision(self, ruleset) -> None:  # type: ignore[no-untyped-def]
        text = "The Supplier shall provide the Services in accordance with Schedule 1."
        assert not any(re.search(u.pattern, text) for u in ruleset.unusual)


class TestInvalidFiles:
    def write(self, tmp_path: Path, mutate) -> Path:  # type: ignore[no-untyped-def]
        raw = yaml.safe_load(RULES.read_text(encoding="utf-8"))
        mutate(raw)
        path = tmp_path / "rules.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return path

    def test_a_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(RulesetError, match="cannot read"):
            load_ruleset(tmp_path / "nope.yaml")

    def test_a_duplicate_rule_id(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, lambda r: r["rules"].append(dict(r["rules"][0])))
        with pytest.raises(RulesetError, match="duplicate rule id"):
            load_ruleset(path)

    def test_a_pattern_that_is_not_a_regular_expression(self, tmp_path: Path) -> None:
        path = self.write(
            tmp_path, lambda r: r["rules"][0]["heuristics"][0].update(pattern="(unclosed")
        )
        with pytest.raises(RulesetError, match="regular expression"):
            load_ruleset(path)

    def test_a_clause_type_the_chunker_cannot_assign(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, lambda r: r["rules"][0].update(clause_types=["made_up_type"]))
        with pytest.raises(RulesetError, match=r"LIAB-001.*made_up_type"):
            load_ruleset(path)

    def test_a_compare_pattern_without_a_group(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, lambda r: r["rules"][4]["compare"][0].update(pattern="days"))
        with pytest.raises(RulesetError, match="capture group"):
            load_ruleset(path)

    def test_a_heuristic_that_does_nothing(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, lambda r: r["rules"][0]["heuristics"][0].update(effect={}))
        with pytest.raises(RulesetError):
            load_ruleset(path)

    def test_a_heuristic_cannot_declare_a_clause_acceptable(self, tmp_path: Path) -> None:
        path = self.write(
            tmp_path,
            lambda r: r["rules"][0]["heuristics"][0].update(effect={"status": "acceptable"}),
        )
        with pytest.raises(RulesetError):
            load_ruleset(path)

    def test_an_unknown_field_is_an_error_not_ignored(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, lambda r: r["rules"][0].update(surprise=1))
        with pytest.raises(RulesetError):
            load_ruleset(path)

    def test_a_bad_version_header(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, lambda r: r.update(semver="one"))
        with pytest.raises(RulesetError):
            load_ruleset(path)
