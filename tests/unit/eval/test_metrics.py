"""Every custom metric of spec 28.3 on toy inputs whose answers can be worked out by hand."""

from typing import ClassVar

import pytest

from eval.dataset import ExpectedFinding, Passage
from eval.metrics import answers, findings, retrieval, stats, vendor
from eval.metrics.answers import AbstentionOutcome, EmittedCitation
from eval.metrics.findings import ProducedFinding
from eval.metrics.text import jaccard, overlaps, similarity

CAP = (
    "The total liability of the Supplier shall not exceed the fees paid in the preceding "
    "twelve months."
)
FRAUD = "The cap shall not apply to fraud or to death caused by negligence."
NOTICE = "The Customer may terminate for convenience on thirty days written notice."


class TestText:
    def test_jaccard_is_the_share_of_shared_words(self) -> None:
        assert jaccard("a b c", "a b d") == pytest.approx(2 / 4)
        assert jaccard("", "") == 0.0 and jaccard("same words", "SAME words") == 1.0

    def test_overlap_threshold_is_inclusive_at_a_half(self) -> None:
        assert not overlaps("a b", "a c")  # one word of three shared: below a half
        # exactly half: {a,b} vs {a,b,c,d} is 2 of 4
        assert overlaps("a b", "a b c d") and not overlaps("a", "a b c")

    def test_similarity_is_levenshtein_ratio(self) -> None:
        assert similarity("hello", "hello") == 1.0 and similarity("abc", "xyz") == 0.0
        assert 0.0 < similarity("hello world", "hello there") < 1.0


class TestStats:
    def test_a_ratio_is_the_sum_of_numerators_over_the_sum_of_denominators(self) -> None:
        assert stats.ratio_of([(1, 2), (3, 4)]) == pytest.approx(
            4 / 6
        )  # micro, not a mean of ratios
        assert stats.ratio_of([]) is None and stats.ratio_of([(0, 0)]) is None

    def test_examples_with_nothing_to_measure_are_not_counted(self) -> None:
        metric = stats.summarise("m", [(1, 1), (0, 0), (0, 1)])
        assert metric.value == 0.5 and metric.n == 2

    def test_the_interval_brackets_the_value_and_is_reproducible(self) -> None:
        pairs = [(1.0, 1.0)] * 8 + [(0.0, 1.0)] * 2
        a, b = stats.summarise("m", pairs), stats.summarise("m", pairs)
        assert a == b and a.ci_low is not None and a.ci_high is not None
        assert a.ci_low <= 0.8 <= a.ci_high and a.ci_low < a.ci_high

    def test_a_metric_with_nothing_measured_has_no_interval(self) -> None:
        metric = stats.summarise("m", [])
        assert metric.value is None and metric.n == 0 and metric.ci_low is None

    def test_perfect_results_have_a_degenerate_interval(self) -> None:
        metric = stats.summarise("m", [(1.0, 1.0)] * 5)
        assert (metric.value, metric.ci_low, metric.ci_high) == (1.0, 1.0, 1.0)

    def test_it_serialises_for_the_report(self) -> None:
        data = stats.summarise(
            "hit_at_5", [(1.0, 1.0), (0.0, 1.0)], higher_is_better=True
        ).to_dict()
        assert data["name"] == "hit_at_5" and data["n"] == 2 and data["value"] == 0.5
        assert len(data["ci95"]) == 2  # type: ignore[arg-type]


class TestRetrieval:
    def test_hit_at_k_depends_on_where_the_passage_is(self) -> None:
        retrieved = ["unrelated boilerplate text here", FRAUD, CAP]
        assert retrieval.hit_at_k(retrieved, [CAP], 3) == (1.0, 1.0)
        assert retrieval.hit_at_k(retrieved, [CAP], 2) == (0.0, 1.0)
        assert retrieval.hit_at_k(retrieved, [CAP], 1) == (0.0, 1.0)

    def test_reciprocal_rank(self) -> None:
        retrieved = ["unrelated", "also unrelated", CAP]
        assert retrieval.reciprocal_rank(retrieved, [CAP]) == (pytest.approx(1 / 3), 1.0)
        assert retrieval.reciprocal_rank([CAP], [CAP]) == (1.0, 1.0)
        assert retrieval.reciprocal_rank(["nothing relevant"], [CAP]) == (0.0, 1.0)

    def test_unanswerable_questions_have_nothing_to_find(self) -> None:
        assert retrieval.hit_at_k([CAP], [], 5) == (0.0, 0.0)
        assert retrieval.reciprocal_rank([CAP], []) == (0.0, 0.0)
        assert retrieval.context_recall([CAP], []) == (0.0, 0.0)
        assert retrieval.context_precision([CAP], []) == (0.0, 0.0)

    def test_the_best_of_several_relevant_passages_counts(self) -> None:
        assert retrieval.hit_at_k([FRAUD], [CAP, FRAUD], 1) == (1.0, 1.0)

    def test_context_precision_is_the_share_of_retrieved_that_match_a_reference(self) -> None:
        retrieved = [CAP, "completely different text about something else entirely"]
        assert retrieval.context_precision(retrieved, [CAP]) == (1.0, 2.0)

    def test_context_recall_is_the_share_of_references_that_were_retrieved(self) -> None:
        assert retrieval.context_recall([CAP], [CAP, NOTICE]) == (1.0, 2.0)
        assert retrieval.context_recall([], [CAP]) == (0.0, 1.0)
        assert retrieval.context_recall([CAP, NOTICE], [CAP, NOTICE]) == (2.0, 2.0)

    def test_precision_with_nothing_retrieved_is_undefined_not_zero(self) -> None:
        assert retrieval.context_precision([], [CAP]) == (0.0, 0.0)


class TestCitations:
    expected: ClassVar[list[Passage]] = [
        Passage(document="acme.txt", page=2, clause_ref="2.1", text=CAP)
    ]

    def cite(self, **kw: object) -> EmittedCitation:
        base: dict[str, object] = {
            "document": "acme.txt",
            "page_start": 2,
            "page_end": 2,
            "text": CAP,
        }
        base.update(kw)
        return EmittedCitation(**base)  # type: ignore[arg-type]

    def test_a_citation_of_the_expected_passage_is_correct(self) -> None:
        assert answers.citation_correctness([self.cite()], self.expected) == (1.0, 1.0)

    @pytest.mark.parametrize(
        "wrong",
        [
            {"document": "other.txt"},  # another document
            {"page_start": 7, "page_end": 7},  # another page
            {"text": FRAUD},  # another passage
        ],
    )
    def test_each_condition_is_needed(self, wrong: dict[str, object]) -> None:
        assert answers.citation_correctness([self.cite(**wrong)], self.expected) == (0.0, 1.0)

    def test_a_page_range_that_contains_the_expected_page_counts(self) -> None:
        assert answers.citation_correctness(
            [self.cite(page_start=1, page_end=3)], self.expected
        ) == (1.0, 1.0)

    def test_an_unknown_page_cannot_rule_a_citation_out(self) -> None:
        assert answers.citation_correctness(
            [self.cite(page_start=None, page_end=None)], self.expected
        ) == (1.0, 1.0)

    def test_the_share_over_several_citations(self) -> None:
        cites = [self.cite(), self.cite(text=FRAUD), self.cite(document="x.txt")]
        assert answers.citation_correctness(cites, self.expected) == (1.0, 3.0)

    def test_no_citations_is_nothing_to_judge(self) -> None:
        assert answers.citation_correctness([], self.expected) == (0.0, 0.0)

    def test_validity_is_the_share_of_markers_that_named_a_real_source(self) -> None:
        assert answers.citation_validity(4, 1) == (3.0, 4.0)
        assert answers.citation_validity(0, 0) == (0.0, 0.0)
        assert answers.citation_validity(2, 5) == (0.0, 2.0)  # never negative


class TestAnswers:
    def test_unsupported_claim_rate(self) -> None:
        assert answers.unsupported_claim_rate(1, 1, 8) == (2.0, 8.0)
        assert answers.unsupported_claim_rate(0, 0, 0) == (0.0, 0.0)

    def test_abstention_outcomes(self) -> None:
        right_answer = AbstentionOutcome(answerable=True, abstained=False)
        right_abstain = AbstentionOutcome(answerable=False, abstained=True)
        false_abstain = AbstentionOutcome(answerable=True, abstained=True)
        false_answer = AbstentionOutcome(answerable=False, abstained=False)
        assert (right_answer.correct, right_abstain.correct) == (True, True)
        assert (false_abstain.correct, false_answer.correct) == (False, False)
        assert false_abstain.false_abstention and not false_abstain.false_answer
        assert false_answer.false_answer and not false_answer.false_abstention

    def test_the_three_rates(self) -> None:
        outcomes = [
            AbstentionOutcome(True, False),
            AbstentionOutcome(True, True),  # false abstention
            AbstentionOutcome(False, True),
            AbstentionOutcome(False, False),  # false answer
            AbstentionOutcome(False, False),  # false answer
        ]
        assert stats.ratio_of(answers.abstention_accuracy(outcomes)) == pytest.approx(2 / 5)
        assert stats.ratio_of(answers.false_abstention_rate(outcomes)) == pytest.approx(1 / 2)
        assert stats.ratio_of(answers.false_answer_rate(outcomes)) == pytest.approx(2 / 3)

    def test_the_rates_are_over_their_own_populations(self) -> None:
        only_answerable = [AbstentionOutcome(True, False)]
        assert answers.false_answer_rate(only_answerable) == []
        assert answers.false_abstention_rate([AbstentionOutcome(False, True)]) == []


def expect(rule: str, status: str, *texts: str) -> ExpectedFinding:
    return ExpectedFinding(
        rule_id=rule, status=status, evidence=[Passage(document="d", text=t) for t in texts]
    )  # type: ignore[arg-type]


def got(rule: str, status: str, *texts: str) -> ProducedFinding:
    return ProducedFinding(rule, status, texts)


class TestFindings:
    def test_clause_extraction_present_needs_the_evidence_to_overlap(self) -> None:
        expected = [expect("LIAB-001", "problematic", CAP)]
        assert findings.clause_extraction_accuracy(
            expected, [got("LIAB-001", "problematic", CAP)]
        ) == (1.0, 1.0)
        assert findings.clause_extraction_accuracy(
            expected, [got("LIAB-001", "problematic", NOTICE)]
        ) == (0.0, 1.0)

    def test_present_matches_present_whatever_the_risk_status(self) -> None:
        expected = [expect("LIAB-001", "acceptable", CAP)]
        assert findings.clause_extraction_accuracy(
            expected, [got("LIAB-001", "ambiguous", CAP)]
        ) == (1.0, 1.0)

    def test_an_absent_clause_must_be_reported_absent(self) -> None:
        expected = [expect("SLA-001", "absent")]
        assert findings.clause_extraction_accuracy(expected, [got("SLA-001", "absent")]) == (
            1.0,
            1.0,
        )
        assert findings.clause_extraction_accuracy(
            expected, [got("SLA-001", "acceptable", CAP)]
        ) == (0.0, 1.0)
        assert findings.clause_extraction_accuracy(
            expected, [got("SLA-001", "insufficient_evidence")]
        ) == (0.0, 1.0)

    def test_a_clause_that_is_there_but_reported_absent_is_a_miss(self) -> None:
        assert findings.clause_extraction_accuracy(
            [expect("A-001", "acceptable", CAP)], [got("A-001", "absent")]
        ) == (0.0, 1.0)

    def test_a_clause_with_no_finding_at_all_is_a_miss(self) -> None:
        assert findings.clause_extraction_accuracy([expect("A-001", "absent")], []) == (0.0, 1.0)

    def test_risk_precision_and_recall(self) -> None:
        expected = [
            expect("A-001", "problematic"),
            expect("A-002", "ambiguous"),
            expect("A-003", "acceptable"),
        ]
        produced = [
            got("A-001", "problematic"),
            got("A-002", "problematic"),
            got("A-003", "ambiguous"),
        ]
        # produced risky: A-001 (right), A-002 (wrong status), A-003 (labelled acceptable): 1 of 3
        assert findings.risk_precision(expected, produced) == (1.0, 3.0)
        # expected risky: A-001 found, A-002 not (wrong status): 1 of 2
        assert findings.risk_recall(expected, produced) == (1.0, 2.0)

    def test_findings_for_unlabelled_rules_do_not_count_against_precision(self) -> None:
        assert findings.risk_precision(
            [expect("A-001", "problematic")],
            [got("A-001", "problematic"), got("Z-999", "problematic")],
        ) == (1.0, 1.0)

    def test_nothing_expected_to_be_risky_has_no_recall(self) -> None:
        assert findings.risk_recall([expect("A-001", "acceptable")], []) == (0.0, 0.0)

    def test_status_accuracy_is_the_exact_status_of_every_labelled_rule(self) -> None:
        expected = [expect("A-001", "problematic"), expect("A-002", "absent")]
        assert findings.status_accuracy(
            expected, [got("A-001", "problematic"), got("A-002", "acceptable")]
        ) == (1.0, 2.0)

    def test_not_expected_pairs_that_appeared_are_counted(self) -> None:
        produced = [got("A-001", "acceptable"), got("A-002", "absent")]
        assert findings.not_expected_violations(
            ["A-001:acceptable", "A-002:acceptable"], produced
        ) == (1.0, 2.0)
        assert findings.not_expected_violations([], produced) == (0.0, 0.0)


class TestVendor:
    def test_signal_agreement(self) -> None:
        expected = {"A": "favorable", "B": "absent", "C": "unfavorable"}
        assert vendor.signal_agreement(
            expected, {"A": "favorable", "B": "absent", "C": "ambiguous"}
        ) == (2.0, 3.0)
        assert vendor.signal_agreement(expected, {}) == (0.0, 3.0)

    def test_dimension_order_claims(self) -> None:
        scores = {
            "Acme": {"legal": 20.0, "data_security": 30.0},
            "Globex": {"legal": 60.0, "data_security": None},
        }
        claims = {"legal": "higher_risk_than:Acme", "data_security": "higher_risk_than:Acme"}
        assert vendor.dimension_order(claims, "Globex", scores) == (1.0, 2.0)  # no score, so false
        assert vendor.dimension_order({"legal": "higher_risk_than:Globex"}, "Acme", scores) == (
            0.0,
            1.0,
        )
        assert vendor.dimension_order({}, "Acme", scores) == (0.0, 0.0)

    def test_determinism_is_all_or_nothing(self) -> None:
        assert vendor.determinism(lambda: {"score": 1.0}) == (1.0, 1.0)
        counter = iter(range(100))
        assert vendor.determinism(lambda: next(counter)) == (0.0, 1.0)
