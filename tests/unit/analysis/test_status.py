"""Spec 20.5 and 20.7: when a clause is absent, problematic, ambiguous, acceptable or unknowable."""

from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest

from covenantiq.ai.schemas import ExtractedValue, RuleAssessment
from covenantiq.analysis.contract.heuristics import run_heuristics
from covenantiq.analysis.contract.rules_loader import load_ruleset
from covenantiq.analysis.contract.status import (
    Evidence,
    ExtractionQualityInfo,
    classify,
    confidence_for,
    severity_for,
    sufficiency_for,
    validated_markers,
)
from covenantiq.analysis.contract.types import SourceRef
from covenantiq.core.enums import ClauseStatus, EvidenceSufficiency, Severity
from covenantiq.rag.config import AnalysisConfig

CONFIG = AnalysisConfig()
GOOD = ExtractionQualityInfo(ok=True, text_ratio=1.0)
RULE = load_ruleset(Path("config/rules/contract_baseline_v1.yaml")).rule("LIAB-001")


def source(
    marker: int, text: str = "Liability is capped at fees paid.", score: float | None = 0.8
) -> SourceRef:
    doc = uuid4()
    return SourceRef(
        marker=marker,
        chunk_ids=[uuid4()],
        document_id=doc,
        document_title="MSA",
        text=text,
        rerank_score=score,
    )


def assessment(**overrides: object) -> RuleAssessment:
    fields: dict[str, object] = {
        "rule_id": "LIAB-001",
        "clause_found": "yes",
        "supporting_markers": [1],
        "status": "acceptable",
        "red_flags": [],
        "extracted_values": [ExtractedValue(name="cap", value="12m")],
        "explanation": "Capped.",
        "business_impact": "",
        "recommended_action": "",
        "confidence": "high",
    }
    fields.update(overrides)
    return RuleAssessment(**fields)  # type: ignore[arg-type]


def decide(
    a: RuleAssessment | None,
    *,
    sources: list[SourceRef] | None = None,
    best: float | None = 0.8,
    taxonomy: bool = False,
    supported: bool = True,
    quality: ExtractionQualityInfo = GOOD,
    text: str | None = None,
):  # type: ignore[no-untyped-def]
    srcs = (
        sources if sources is not None else [source(1, text or "Liability is capped at fees paid.")]
    )
    hits = run_heuristics(RULE, srcs)
    evidence = Evidence(srcs, best, taxonomy, hits, a, supported)
    return classify(evidence, quality, CONFIG)


class TestPresentClauses:
    def test_found_with_evidence_and_nothing_wrong_is_acceptable(self) -> None:
        result = decide(assessment())
        assert result.status is ClauseStatus.ACCEPTABLE and result.markers == [1]

    def test_the_models_problematic_needs_a_cited_passage(self) -> None:
        assert decide(assessment(status="problematic")).status is ClauseStatus.PROBLEMATIC
        no_evidence = decide(assessment(status="problematic", supporting_markers=[9]))
        assert no_evidence.status is ClauseStatus.INSUFFICIENT_EVIDENCE

    def test_ambiguous(self) -> None:
        assert decide(assessment(status="ambiguous")).status is ClauseStatus.AMBIGUOUS

    def test_acceptable_with_red_flags_is_downgraded_to_ambiguous(self) -> None:
        result = decide(assessment(red_flags=["cap basis undefined"]))
        assert result.status is ClauseStatus.AMBIGUOUS and result.reasons

    def test_low_confidence_is_never_a_verdict(self) -> None:
        assert decide(assessment(status="problematic", confidence="low")).status is (
            ClauseStatus.INSUFFICIENT_EVIDENCE
        )

    def test_an_explanation_the_sources_do_not_support_is_not_trusted(self) -> None:
        assert decide(assessment(), supported=False).status is ClauseStatus.INSUFFICIENT_EVIDENCE

    def test_unclear_with_no_pattern_hit_is_insufficient(self) -> None:
        result = decide(
            assessment(
                clause_found="unclear", supporting_markers=[], status="insufficient_evidence"
            )
        )
        assert result.status is ClauseStatus.INSUFFICIENT_EVIDENCE

    def test_a_status_that_contradicts_having_found_the_clause_is_insufficient(self) -> None:
        assert decide(assessment(status="absent")).status is ClauseStatus.INSUFFICIENT_EVIDENCE

    def test_markers_that_name_no_given_source_are_dropped(self) -> None:
        result = decide(assessment(supporting_markers=[1, 7, 1]))
        assert result.markers == [1]


class TestHeuristics:
    UNLIMITED = "The Supplier accepts unlimited liability for any loss."

    def test_a_forced_red_flag_decides_problematic_whatever_the_model_said(self) -> None:
        result = decide(assessment(status="acceptable"), text=self.UNLIMITED)
        assert result.status is ClauseStatus.PROBLEMATIC and result.forced

    def test_a_forced_red_flag_beats_low_confidence_and_an_unsupported_explanation(self) -> None:
        result = decide(assessment(confidence="low"), text=self.UNLIMITED, supported=False)
        assert result.status is ClauseStatus.PROBLEMATIC

    def test_a_hit_means_the_clause_is_there_even_if_the_model_said_no(self) -> None:
        result = decide(
            assessment(clause_found="no", supporting_markers=[], status="absent"),
            text=self.UNLIMITED,
        )
        assert result.status is ClauseStatus.PROBLEMATIC and result.markers == [1]

    def test_a_flag_without_a_forced_status_stops_acceptable(self) -> None:
        text = "The cap shall not apply to breaches of data protection obligations."
        assert decide(assessment(), text=text).status is ClauseStatus.AMBIGUOUS

    def test_no_heuristic_can_produce_acceptable(self) -> None:
        # nothing matches, the model is unsure: a clean scan never upgrades a clause
        result = decide(assessment(clause_found="unclear", supporting_markers=[]))
        assert result.status is not ClauseStatus.ACCEPTABLE


class TestAbsence:
    """Absence is a claim that needs evidence of search (spec 20.5)."""

    NOT_FOUND: ClassVar[dict[str, object]] = dict(
        clause_found="no", supporting_markers=[], status="absent"
    )

    def test_confident_no_with_a_weak_search_and_no_tagged_clause_is_absent(self) -> None:
        result = decide(assessment(**self.NOT_FOUND), sources=[], best=0.1)
        assert result.status is ClauseStatus.ABSENT and len(result.reasons) == 2

    def test_a_confident_model_alone_is_enough_when_the_score_is_unknown(self) -> None:
        result = decide(assessment(**self.NOT_FOUND), sources=[], best=None)
        assert result.status is ClauseStatus.ABSENT

    def test_a_low_score_alone_is_enough_when_the_model_is_unsure(self) -> None:
        result = decide(assessment(**self.NOT_FOUND, confidence="low"), sources=[], best=0.05)
        assert result.status is ClauseStatus.ABSENT

    def test_unsure_model_and_no_score_to_lean_on_is_insufficient(self) -> None:
        result = decide(assessment(**self.NOT_FOUND, confidence="low"), sources=[], best=None)
        assert result.status is ClauseStatus.INSUFFICIENT_EVIDENCE

    def test_a_tagged_clause_of_that_type_blocks_absence(self) -> None:
        result = decide(assessment(**self.NOT_FOUND), sources=[], best=0.1, taxonomy=True)
        assert result.status is ClauseStatus.INSUFFICIENT_EVIDENCE

    def test_poor_extraction_blocks_absence(self) -> None:
        bad = ExtractionQualityInfo(ok=False, text_ratio=0.6)
        result = decide(assessment(**self.NOT_FOUND), sources=[], best=0.0, quality=bad)
        assert result.status is ClauseStatus.INSUFFICIENT_EVIDENCE
        assert "extracted completely" in result.reasons[0]

    def test_a_model_that_says_no_while_the_search_was_strong_and_unsure_is_insufficient(
        self,
    ) -> None:
        result = decide(assessment(**self.NOT_FOUND, confidence="low"), best=0.9)
        assert result.status is ClauseStatus.INSUFFICIENT_EVIDENCE

    def test_the_threshold_comes_from_config(self) -> None:
        strict = AnalysisConfig(absent_threshold=0.9)
        evidence = Evidence(
            [], 0.5, False, [], assessment(**self.NOT_FOUND, confidence="low"), True
        )
        assert classify(evidence, GOOD, strict).status is ClauseStatus.ABSENT
        assert classify(evidence, GOOD, CONFIG).status is ClauseStatus.INSUFFICIENT_EVIDENCE


class TestUnavailable:
    def test_no_assessment_is_insufficient(self) -> None:
        assert decide(None).status is ClauseStatus.INSUFFICIENT_EVIDENCE

    def test_poor_extraction_makes_everything_insufficient(self) -> None:
        bad = ExtractionQualityInfo(ok=False, text_ratio=0.4)
        assert decide(assessment(), quality=bad).status is ClauseStatus.INSUFFICIENT_EVIDENCE


class TestExtractionQuality:
    @pytest.mark.parametrize(
        ("quality", "ok"),
        [
            ({"pages_total": 20, "pages_with_text": 20, "insufficient_text": False}, True),
            ({"pages_total": 20, "pages_with_text": 19, "insufficient_text": False}, True),
            ({"pages_total": 20, "pages_with_text": 18, "insufficient_text": False}, False),
            ({"pages_total": 20, "pages_with_text": 20, "insufficient_text": True}, False),
            ({"pages_total": None, "pages_with_text": None, "insufficient_text": False}, True),
        ],
    )
    def test_the_ninety_five_percent_rule(self, quality: dict[str, object], ok: bool) -> None:
        assert ExtractionQualityInfo.from_document(quality, CONFIG).ok is ok


class TestSeverity:
    def classification(self, status: ClauseStatus):  # type: ignore[no-untyped-def]
        from covenantiq.analysis.contract.status import Classification

        return Classification(status)

    def test_the_rules_policy_sets_the_base(self) -> None:
        assert severity_for(RULE, self.classification(ClauseStatus.ABSENT), []) is Severity.HIGH
        assert (
            severity_for(RULE, self.classification(ClauseStatus.AMBIGUOUS), []) is Severity.MEDIUM
        )
        assert (
            severity_for(RULE, self.classification(ClauseStatus.PROBLEMATIC), []) is Severity.HIGH
        )

    def test_acceptable_and_unknown_are_info(self) -> None:
        for status in (ClauseStatus.ACCEPTABLE, ClauseStatus.INSUFFICIENT_EVIDENCE):
            assert severity_for(RULE, self.classification(status), []) is Severity.INFO

    def test_a_heuristic_raises_but_never_lowers(self) -> None:
        hits = run_heuristics(RULE, [source(1, "unlimited liability applies")])
        assert (
            severity_for(RULE, self.classification(ClauseStatus.PROBLEMATIC), hits)
            is Severity.CRITICAL
        )
        low = run_heuristics(
            load_ruleset(Path("config/rules/contract_baseline_v1.yaml")).rule("PAY-003"),
            [source(1, "interest of 18% per annum")],
        )
        pay = load_ruleset(Path("config/rules/contract_baseline_v1.yaml")).rule("PAY-003")
        assert (
            severity_for(pay, self.classification(ClauseStatus.PROBLEMATIC), low) is Severity.MEDIUM
        )
        # a hit never lowers an already higher base
        assert (
            severity_for(RULE, self.classification(ClauseStatus.PROBLEMATIC), low) is Severity.HIGH
        )


class TestConfidenceAndSufficiency:
    def test_the_formula(self) -> None:
        # 0.5 * 0.85 + 0.3 * 0.8 + 0.2 * 1.0
        assert confidence_for(assessment(confidence="high"), 0.8, GOOD) == pytest.approx(0.865)
        assert confidence_for(
            assessment(confidence="low"), 0.0, ExtractionQualityInfo(True, 0.5)
        ) == pytest.approx(0.275)

    def test_an_unknown_score_counts_as_no_evidence_strength_and_no_assessment_as_no_confidence(
        self,
    ) -> None:
        assert confidence_for(None, None, GOOD) == pytest.approx(0.2)

    def test_scores_are_clipped(self) -> None:
        assert confidence_for(assessment(confidence="high"), 5.0, GOOD) == pytest.approx(0.925)

    @pytest.mark.parametrize(
        ("scores", "markers", "claimed", "expected"),
        [
            ([0.5, 0.4], [1, 2], 2, EvidenceSufficiency.STRONG),
            ([0.75], [1], 1, EvidenceSufficiency.STRONG),
            ([0.5], [1], 1, EvidenceSufficiency.MODERATE),
            ([0.9], [], 2, EvidenceSufficiency.WEAK),
            ([0.9], [], 0, EvidenceSufficiency.NONE),
        ],
    )
    def test_sufficiency(
        self, scores: list[float], markers: list[int], claimed: int, expected: EvidenceSufficiency
    ) -> None:
        sources = [source(i + 1, score=s) for i, s in enumerate(scores)]
        assert sufficiency_for(markers, sources, claimed=claimed) is expected

    def test_validated_markers_keep_order_and_drop_strangers(self) -> None:
        assert validated_markers([3, 1, 3, 9], [source(1), source(3)]) == [3, 1]
