"""Heuristics, contradiction candidates, obligations and finding drafts."""

from pathlib import Path
from uuid import uuid4

import pytest

from covenantiq.ai.schemas import ObligationItem
from covenantiq.analysis.contract import findings as f
from covenantiq.analysis.contract.contradictions import find_candidates, normalise, observe
from covenantiq.analysis.contract.heuristics import forced_problem, run_heuristics
from covenantiq.analysis.contract.obligations import validate_obligations
from covenantiq.analysis.contract.rules_loader import load_ruleset
from covenantiq.analysis.contract.types import (
    ContradictionRecord,
    FindingDraft,
    RuleResult,
    SourceRef,
)
from covenantiq.core.enums import ClauseStatus, EvidenceSufficiency, RiskCategory, Severity

RULES = load_ruleset(Path("config/rules/contract_baseline_v1.yaml"))


def src(marker: int, text: str, ref: str | None = "5.1") -> SourceRef:
    return SourceRef(
        marker=marker,
        chunk_ids=[uuid4()],
        clause_id=uuid4(),
        document_id=uuid4(),
        document_title="Acme MSA",
        clause_ref=ref,
        page_start=3,
        page_end=3,
        text=text,
    )


class TestHeuristicEngine:
    def test_hits_say_which_source_and_what_matched(self) -> None:
        rule = RULES.rule("LIAB-001")
        hits = run_heuristics(
            rule,
            [
                src(1, "Fees are payable monthly."),
                src(2, "There is unlimited liability for IP claims."),
            ],
        )
        assert [(h.heuristic_id, h.marker) for h in hits] == [("UNLIMITED", 2)]
        assert "unlimited liability" in hits[0].span and hits[0].chunk_id is not None
        assert forced_problem(hits) == hits

    def test_a_flag_only_hit_is_not_a_forced_problem(self) -> None:
        hits = run_heuristics(
            RULES.rule("LIAB-001"),
            [src(1, "This cap shall not apply to data protection breaches.")],
        )
        assert [h.flag for h in hits] == ["carve_out_data"] and forced_problem(hits) == []

    def test_one_hit_per_heuristic_per_source(self) -> None:
        text = "unlimited liability. unlimited liability. unlimited liability."
        assert len(run_heuristics(RULES.rule("LIAB-001"), [src(1, text)])) == 1

    def test_a_very_long_passage_is_scanned_only_in_part(self) -> None:
        text = ("x " * 6000) + "unlimited liability"
        assert run_heuristics(RULES.rule("LIAB-001"), [src(1, text)]) == []


class TestContradictionCandidates:
    def observations(self, *texts: str):  # type: ignore[no-untyped-def]
        rule = RULES.rule("PAY-001")
        return [o for i, t in enumerate(texts, 1) for o in observe(rule, [src(i, t)])]

    def test_values_are_normalised(self) -> None:
        assert normalise(" 030 ", "days") == "30"
        assert normalise("The State of New York", "text") == "new york"
        assert normalise("England & Wales", "text") == "england and wales"
        assert normalise("days", "days") is None

    def test_two_different_payment_periods_in_different_clauses_are_a_candidate(self) -> None:
        obs = self.observations(
            "Invoices are payable within 30 days.",
            "Payment is due within 45 days of the invoice date.",
        )
        pairs = find_candidates(obs, limit=5)
        assert len(pairs) == 1 and {pairs[0][0].value, pairs[0][1].value} == {"30", "45"}

    def test_the_same_value_twice_is_not(self) -> None:
        obs = self.observations(
            "Invoices are payable within 30 days.", "Payment is due within 30 days."
        )
        assert find_candidates(obs, limit=5) == []

    def test_two_values_in_one_passage_are_not_a_contradiction(self) -> None:
        obs = self.observations(
            "Invoices are payable within 30 days; disputed amounts within 10 days."
        )
        assert find_candidates(obs, limit=5) == []

    def test_governing_law(self) -> None:
        rule = RULES.rule("GOV-001")
        obs = observe(
            rule, [src(1, "This Agreement is governed by the laws of England and Wales.")]
        ) + observe(rule, [src(2, "The Schedule is governed by the laws of New York.")])
        pairs = find_candidates(obs, limit=5)
        assert len(pairs) == 1 and pairs[0][0].key == "governing_law"

    def test_the_limit_is_shared_across_keys(self) -> None:
        pay = RULES.rule("PAY-001")
        gov = RULES.rule("GOV-001")
        obs = (
            [
                o
                for i, v in enumerate((10, 20, 30, 40), 1)
                for o in observe(pay, [src(i, f"payable within {v} days")])
            ]
            + observe(gov, [src(9, "governed by the laws of France.")])
            + observe(gov, [src(10, "governed by the laws of Japan.")])
        )
        pairs = find_candidates(obs, limit=3)
        assert {p[0].key for p in pairs} == {"payment_days", "governing_law"}

    def test_order_is_deterministic(self) -> None:
        obs = self.observations("payable within 30 days", "due within 45 days", "net 60 days")
        assert [(a.value, b.value) for a, b in find_candidates(obs, limit=5)] == [
            (a.value, b.value) for a, b in find_candidates(obs, limit=5)
        ]


class TestObligations:
    def item(self, **kw: object) -> ObligationItem:
        base = dict(
            party="vendor",
            action="notify the customer of a breach",
            deadline_text="within 72 hours",
            deadline_days=None,
            trigger=None,
            clause_ref=None,
            marker=1,
        )
        base.update(kw)
        return ObligationItem(**base)  # type: ignore[arg-type]

    def test_an_obligation_must_cite_a_given_source(self) -> None:
        records = validate_obligations(
            [self.item(marker=1), self.item(marker=4, action="other")],
            [src(1, "The vendor shall notify.")],
        )
        assert (
            len(records) == 1 and records[0].source.marker == 1 and records[0].clause_ref == "5.1"
        )

    def test_an_invented_deadline_loses_the_number_not_the_obligation(self) -> None:
        records = validate_obligations(
            [
                self.item(deadline_days=30, deadline_text="within 30 days"),
                self.item(action="pay invoices", deadline_days=14),
            ],
            [src(1, "The customer shall give notice no later than 14 days before renewal.")],
        )
        by_action = {r.action: r for r in records}
        assert by_action["notify the customer of a breach"].deadline_days is None
        assert by_action["pay invoices"].deadline_days == 14

    def test_duplicates_and_empty_items_are_dropped(self) -> None:
        records = validate_obligations(
            [self.item(), self.item(), self.item(action="  ")], [src(1, "text")]
        )
        assert len(records) == 1

    def test_long_fields_are_shortened(self) -> None:
        records = validate_obligations([self.item(action="x " * 500)], [src(1, "text")])
        assert len(records[0].action) <= 400


class TestFindingDrafts:
    def result(self, status: ClauseStatus, **kw: object) -> RuleResult:
        base = dict(
            rule_id="LIAB-001",
            title="Limitation of liability",
            category=RiskCategory.LIABILITY,
            status=status,
            severity=Severity.HIGH,
            confidence=0.8,
            evidence_sufficiency=EvidenceSufficiency.STRONG,
            explanation="x " * 1000,
            sources=[src(1, "text")],
        )
        base.update(kw)
        return RuleResult(**base)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("status", "title"),
        [
            (ClauseStatus.ABSENT, "No limitation of liability found"),
            (ClauseStatus.PROBLEMATIC, "Limitation of liability: terms that create risk"),
            (ClauseStatus.AMBIGUOUS, "Limitation of liability: unclear or incomplete"),
            (ClauseStatus.INSUFFICIENT_EVIDENCE, "Limitation of liability: could not be assessed"),
            (ClauseStatus.ACCEPTABLE, "Limitation of liability: within baseline"),
        ],
    )
    def test_titles(self, status: ClauseStatus, title: str) -> None:
        assert f.draft_from_rule(self.result(status)).title == title

    def test_text_is_clipped_and_the_evidence_travels_with_the_finding(self) -> None:
        draft = f.draft_from_rule(self.result(ClauseStatus.PROBLEMATIC, business_impact="y " * 900))
        assert len(draft.explanation) <= 1200 and len(draft.business_impact or "") <= 800
        assert len(draft.evidence) == 1 and draft.details["kind"] == "rule"

    def test_an_error_is_recorded_in_the_details(self) -> None:
        draft = f.draft_from_rule(
            self.result(ClauseStatus.INSUFFICIENT_EVIDENCE, error="LLM_UNAVAILABLE")
        )
        assert draft.details["error"] == "LLM_UNAVAILABLE"

    def test_absence_says_what_was_searched(self) -> None:
        text = f.absent_explanation(
            "Service levels", {"queries_run": 2, "pages_scanned": 42, "best_rerank": 0.12}
        )
        assert "2 targeted searches across 42 pages" in text and "0.12" in text

    def test_ordinals_count_per_rule(self) -> None:
        drafts = [
            FindingDraft(
                rule_id=r,
                risk_category=RiskCategory.GENERAL,
                clause_status=ClauseStatus.PROBLEMATIC,
                severity=Severity.LOW,
                title="t",
                explanation="e",
                confidence=0.5,
                evidence_sufficiency=EvidenceSufficiency.WEAK,
            )
            for r in ("A-001", "UNUSUAL-X", "UNUSUAL-X", "A-001")
        ]
        assert [(d.rule_id, d.ordinal) for d in f.number_ordinals(drafts)] == [
            ("A-001", 0),
            ("UNUSUAL-X", 0),
            ("UNUSUAL-X", 1),
            ("A-001", 1),
        ]

    def test_summary_counts(self) -> None:
        drafts = [
            f.draft_from_rule(self.result(s))
            for s in (ClauseStatus.ABSENT, ClauseStatus.ABSENT, ClauseStatus.ACCEPTABLE)
        ]
        stats = f.summarise(drafts)
        assert stats["by_status"] == {"absent": 2, "acceptable": 1} and stats["by_severity"] == {
            "high": 3
        }

    def test_a_contradiction_cites_both_passages(self) -> None:
        pay = RULES.rule("PAY-001")
        a = observe(pay, [src(1, "payable within 30 days")])[0]
        b = observe(pay, [src(2, "due within 45 days", ref="9.2")])[0]
        draft = f.draft_from_contradiction(
            ContradictionRecord(key="payment_days", first=a, second=b, explanation="30 vs 45"), 0
        )
        assert draft.rule_id == "CONTRA-PAYMENT-DAYS" and len(draft.evidence) == 2
        assert draft.details["values"] == ["30", "45"]

    def test_the_disclaimer_is_the_spec_text(self) -> None:
        assert f.DISCLAIMER.startswith("Automated analysis for decision support. Not legal advice.")
