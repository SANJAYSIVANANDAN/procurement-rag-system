"""Comparing vendors (spec 21.8): where a comparison is allowed, and where it must say it is not."""

from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st

from covenantiq.analysis.vendor.comparison import (
    POLICY_LABEL,
    AssessmentSnapshot,
    DimensionSnapshot,
    SignalSnapshot,
    compare,
    template_summary,
    validate_narrative,
)
from covenantiq.analysis.vendor.config_schema import get_vendor_config
from covenantiq.analysis.vendor.scoring import score_assessment
from covenantiq.analysis.vendor.types import SignalAggregate
from covenantiq.core.enums import RiskDimension, Severity

CONFIG = get_vendor_config()
SCORING, TAXONOMY = CONFIG.scoring, CONFIG.signals
ACME, GLOBEX, INITECH = UUID(int=11), UUID(int=12), UUID(int=13)
KNOWN = {s.id for s in TAXONOMY.signals}


def snapshot(vendor: UUID, name: str, statuses: dict[str, str]) -> AssessmentSnapshot:
    """An assessment built the real way: statuses -> aggregates -> the scoring engine."""
    aggregates = {
        sid: SignalAggregate(
            signal_id=sid,
            dimension=TAXONOMY.by_id()[sid].dimension,
            status=("unfavorable" if label.startswith("unfav") else label),  # type: ignore[arg-type]
            severity=Severity.HIGH if label.startswith("unfav") else None,
        )
        for sid, label in statuses.items()
    }
    score = score_assessment(SCORING, TAXONOMY, aggregates)
    signals = [
        SignalSnapshot(
            signal_id=s.signal_id,
            dimension=d.dimension,
            label=s.label,
            status=aggregates[s.signal_id].status
            if s.signal_id in aggregates
            else "insufficient_evidence",
            policy_treated=s.policy_treated,
        )
        for d in score.dimensions
        for s in d.signals
    ]
    return AssessmentSnapshot(
        vendor_id=vendor,
        vendor_name=name,
        assessment_id=UUID(int=vendor.int + 1000),
        overall_score=score.overall_score,
        provisional=score.provisional,
        dimensions=[
            DimensionSnapshot(
                dimension=d.dimension,
                status=d.status,
                score=d.score,
                severity=d.severity,
                coverage=d.coverage,
            )
            for d in score.dimensions
        ],
        signals=signals,
    )


def everything(label: str, **overrides: str) -> dict[str, str]:
    return {**{s.id: label for s in TAXONOMY.signals}, **overrides}


class TestDimensions:
    def test_complete_assessments_are_compared_and_the_lower_risk_leads(self) -> None:
        result = compare(
            SCORING,
            [
                snapshot(ACME, "Acme", everything("favorable")),
                snapshot(GLOBEX, "Globex", everything("ambiguous")),
            ],
        )
        assert all(d["comparable"] for d in result["dimensions"])
        assert {d["leader"] for d in result["dimensions"]} == {str(ACME)}

    def test_equal_scores_have_no_leader(self) -> None:
        result = compare(
            SCORING,
            [
                snapshot(ACME, "Acme", everything("favorable")),
                snapshot(GLOBEX, "Globex", everything("favorable")),
            ],
        )
        assert all(d["comparable"] and d["leader"] is None for d in result["dimensions"])

    def test_a_dimension_one_vendor_has_little_evidence_for_is_not_comparable_and_says_why(
        self,
    ) -> None:
        thin = everything("favorable")
        for s in TAXONOMY.for_dimension(RiskDimension.DATA_SECURITY):
            if s.id not in ("DS-DPA", "DS-PEN-TEST"):
                del thin[s.id]  # leaves DPA (3) + pen-test (1) of 11: coverage 0.36
        result = compare(
            SCORING,
            [snapshot(ACME, "Acme", everything("favorable")), snapshot(GLOBEX, "Globex", thin)],
        )
        row = next(d for d in result["dimensions"] if d["dimension"] == "data_security")
        assert not row["comparable"] and row["leader"] is None
        assert "Globex: not assessed" in row["reason"] or "Globex: coverage" in row["reason"]
        assert row["cells"][str(GLOBEX)]["coverage"] < 0.6

    def test_a_dimension_with_nothing_is_not_comparable(self) -> None:
        none_for_compliance = {
            k: v for k, v in everything("favorable").items() if not k.startswith("CMP-")
        }
        result = compare(
            SCORING,
            [
                snapshot(ACME, "Acme", everything("favorable")),
                snapshot(GLOBEX, "Globex", none_for_compliance),
            ],
        )
        row = next(d for d in result["dimensions"] if d["dimension"] == "compliance")
        assert not row["comparable"] and "Globex: not assessed" in row["reason"]


class TestSignals:
    def row(self, result: dict, signal: str) -> dict:  # type: ignore[type-arg]
        return next(s for s in result["signals"] if s["signal_id"] == signal)

    def test_two_evidence_based_statuses_are_comparable_whatever_they_are(self) -> None:
        result = compare(
            SCORING,
            [
                snapshot(ACME, "Acme", everything("favorable")),
                snapshot(GLOBEX, "Globex", everything("favorable", **{"OPS-SLA": "unfav"})),
            ],
        )
        row = self.row(result, "OPS-SLA")
        assert row["comparable"] and row["cells"][str(GLOBEX)]["status"] == "unfavorable"

    def test_a_clause_against_an_absence_is_never_compared(self) -> None:
        result = compare(
            SCORING,
            [
                snapshot(ACME, "Acme", everything("favorable")),
                snapshot(GLOBEX, "Globex", everything("favorable", **{"OPS-SLA": "absent"})),
            ],
        )
        row = self.row(result, "OPS-SLA")
        assert not row["comparable"]
        assert row["reason"] == "Missing in Globex — cannot compare"

    def test_an_absence_the_policy_counts_as_unfavorable_is_compared_and_labelled(self) -> None:
        result = compare(
            SCORING,
            [
                snapshot(ACME, "Acme", everything("favorable")),
                snapshot(GLOBEX, "Globex", everything("favorable", **{"LEG-LIAB-CAP": "absent"})),
            ],
        )
        row = self.row(result, "LEG-LIAB-CAP")
        assert row["comparable"] and row["cells"][str(GLOBEX)]["label"] == POLICY_LABEL

    def test_absent_everywhere_is_comparable_there_is_nothing_to_rank(self) -> None:
        result = compare(
            SCORING,
            [
                snapshot(ACME, "Acme", everything("favorable", **{"OPS-SLA": "absent"})),
                snapshot(GLOBEX, "Globex", everything("favorable", **{"OPS-SLA": "absent"})),
            ],
        )
        assert self.row(result, "OPS-SLA")["comparable"]

    def test_unknown_evidence_on_either_side_is_not_comparable(self) -> None:
        result = compare(
            SCORING,
            [
                snapshot(
                    ACME, "Acme", everything("favorable", **{"OPS-SLA": "insufficient_evidence"})
                ),
                snapshot(
                    GLOBEX,
                    "Globex",
                    everything("favorable", **{"OPS-SLA": "insufficient_evidence"}),
                ),
            ],
        )
        assert not self.row(result, "OPS-SLA")["comparable"]

    def test_three_vendors_name_every_vendor_that_lacks_the_evidence(self) -> None:
        result = compare(
            SCORING,
            [
                snapshot(ACME, "Acme", everything("favorable")),
                snapshot(GLOBEX, "Globex", everything("favorable", **{"OPS-SLA": "absent"})),
                snapshot(
                    INITECH,
                    "Initech",
                    everything("favorable", **{"OPS-SLA": "insufficient_evidence"}),
                ),
            ],
        )
        assert (
            self.row(result, "OPS-SLA")["reason"] == "Missing in Globex, Initech — cannot compare"
        )


class TestRanking:
    def test_complete_assessments_are_ranked_lowest_risk_first(self) -> None:
        result = compare(
            SCORING,
            [
                snapshot(GLOBEX, "Globex", everything("ambiguous")),
                snapshot(ACME, "Acme", everything("favorable")),
            ],
        )
        assert (
            result["ranking"] == [str(ACME), str(GLOBEX)]
            and result["ranking_suppressed_reason"] is None
        )

    def test_a_provisional_assessment_suppresses_the_ranking(self) -> None:
        sparse = {k: v for k, v in everything("favorable").items() if k.startswith(("LEG-", "DS-"))}
        result = compare(
            SCORING,
            [snapshot(ACME, "Acme", everything("favorable")), snapshot(GLOBEX, "Globex", sparse)],
        )
        assert (
            result["ranking"] == []
            and "provisional" in result["ranking_suppressed_reason"]
            and "Globex" in result["ranking_suppressed_reason"]
        )

    def test_too_few_comparable_dimensions_suppress_it_even_for_complete_vendors(self) -> None:
        # both are assessed in four or more dimensions and are not provisional
        a = everything("favorable")
        b = {k: v for k, v in a.items() if not k.startswith("DEP-")}
        c = {k: v for k, v in b.items() if not k.startswith("CMP-")}
        result = compare(SCORING, [snapshot(ACME, "Acme", a), snapshot(GLOBEX, "Globex", c)])
        comparable = sum(1 for d in result["dimensions"] if d["comparable"])
        assert comparable == 4
        thin = compare(
            SCORING.model_copy(
                update={
                    "comparison": SCORING.comparison.model_copy(
                        update={"min_comparable_dimensions_to_rank": 5}
                    )
                }
            ),
            [snapshot(ACME, "Acme", a), snapshot(GLOBEX, "Globex", c)],
        )
        assert (
            thin["ranking"] == []
            and "only 4 dimensions are comparable" in thin["ranking_suppressed_reason"]
        )

    def test_the_result_names_the_config_version_and_each_assessment(self) -> None:
        result = compare(
            SCORING,
            [
                snapshot(ACME, "Acme", everything("favorable")),
                snapshot(GLOBEX, "Globex", everything("favorable")),
            ],
        )
        assert result["scoring_config_version"] == "vendor_scoring_v1@1.0.0"
        assert [v["name"] for v in result["vendors"]] == ["Acme", "Globex"]
        assert all(v["assessment_id"] for v in result["vendors"])

    @settings(max_examples=40, deadline=None)
    @given(st.permutations([0, 1, 2]))
    def test_the_order_the_vendors_are_given_in_does_not_matter(self, order: list[int]) -> None:
        pool = [
            snapshot(ACME, "Acme", everything("favorable")),
            snapshot(GLOBEX, "Globex", everything("ambiguous", **{"OPS-SLA": "absent"})),
            snapshot(INITECH, "Initech", everything("favorable", **{"DS-DPA": "unfav"})),
        ]
        assert compare(SCORING, [pool[i] for i in order]) == compare(SCORING, pool)


class TestNarrative:
    def result(self) -> dict:  # type: ignore[type-arg]
        """Globex is provisional (only legal and data security evidence), so there is no ranking."""
        sparse = {k: v for k, v in everything("favorable").items() if k.startswith(("LEG-", "DS-"))}
        return compare(
            SCORING,
            [snapshot(ACME, "Acme", everything("favorable")), snapshot(GLOBEX, "Globex", sparse)],
        )

    GOOD = (
        "Acme and Globex were assessed against the same scoring configuration. Acme shows lower "
        "risk "
        "in the legal dimension and Globex has the same data_security position, with both vendors "
        "showing favorable terms for the signals LEG-LIAB-CAP and DS-DPA. The two vendors cannot "
        "be "
        "compared on compliance because Globex has too little evidence there, so no conclusion is "
        "drawn for that dimension. Operational terms are favorable on both sides, and the "
        "financial "
        "commercial terms are likewise comparable, with no differences that change the picture. "
        "Dependency terms are also comparable. The remaining signals match across both contracts, "
        "so the overall position is close, and the reader should review the evidence behind each "
        "dimension before relying on this summary or on any single score."
    )

    def test_a_sound_narrative_passes(self) -> None:
        assert validate_narrative(self.GOOD, self.result(), KNOWN) == []

    def test_length_is_enforced(self) -> None:
        assert any(
            "length" in p for p in validate_narrative("Acme is better.", self.result(), KNOWN)
        )
        assert any("length" in p for p in validate_narrative("word " * 400, self.result(), KNOWN))

    def test_an_invented_signal_id_is_rejected(self) -> None:
        text = self.GOOD + " The signal XYZ-MAGIC differs."
        assert "unknown signal id XYZ-MAGIC" in validate_narrative(text, self.result(), KNOWN)

    def test_a_comparison_of_a_not_comparable_dimension_is_rejected(self) -> None:
        text = self.GOOD + " Acme is clearly better than Globex on compliance."
        assert any(
            "compliance, which is not comparable" in p
            for p in validate_narrative(text, self.result(), KNOWN)
        )

    def test_mentioning_a_not_comparable_dimension_without_comparing_is_fine(self) -> None:
        assert validate_narrative(self.GOOD, self.result(), KNOWN) == []

    def test_a_ranking_claim_without_a_ranking_is_rejected(self) -> None:
        assert self.result()["ranking"] == []
        text = self.GOOD + " Overall, Acme ranks higher than Globex."
        assert any("ranks the vendors" in p for p in validate_narrative(text, self.result(), KNOWN))

    def test_saying_that_there_is_no_ranking_is_fine(self) -> None:
        text = self.GOOD + " No overall ranking is given because Globex is provisional."
        assert validate_narrative(text, self.result(), KNOWN) == []

    def test_the_template_fallback_is_deterministic_and_always_valid_content(self) -> None:
        result = self.result()
        text = template_summary(result)
        assert text == template_summary(result)
        assert "compliance: not comparable" in text and "No overall ranking" in text
        assert "Acme" in text and "Globex" in text

    def test_the_template_names_the_ranking_when_there_is_one(self) -> None:
        result = compare(
            SCORING,
            [
                snapshot(ACME, "Acme", everything("favorable")),
                snapshot(GLOBEX, "Globex", everything("ambiguous")),
            ],
        )
        assert "Acme (10.0) < Globex (50.0)" in template_summary(result)
