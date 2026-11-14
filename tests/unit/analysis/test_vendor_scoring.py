"""Signal mapping, aggregation and the scoring engine, including the properties of spec 21.5."""

import random
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from covenantiq.analysis.vendor.aggregation import aggregate_all, aggregate_signal
from covenantiq.analysis.vendor.config_schema import (
    SignalsConfig,
    SignalSpec,
    get_vendor_config,
)
from covenantiq.analysis.vendor.scoring import (
    effective_weights,
    score_assessment,
    score_dimension,
    score_signal,
)
from covenantiq.analysis.vendor.signals import FindingFacts, map_contract_signals, observe_document
from covenantiq.analysis.vendor.types import EvidenceRef, Observation, SignalAggregate
from covenantiq.core.enums import ClauseStatus, DimensionStatus, RiskDimension, Severity

CONFIG = get_vendor_config()
SCORING, TAXONOMY = CONFIG.scoring, CONFIG.signals
SPECS = TAXONOMY.signals
DOC_A, DOC_B, DOC_C = (UUID(int=i) for i in (1, 2, 3))


def evidence(doc: UUID = DOC_A, quote: str = "text") -> EvidenceRef:
    return EvidenceRef(document_id=doc, document_title="Doc", quote=quote, page_start=1)


def obs(
    signal: str, status: str, doc: UUID = DOC_A, severity: Severity | None = None
) -> Observation:
    return Observation(
        signal_id=signal,
        document_id=doc,
        status=status,
        severity=severity,  # type: ignore[arg-type]
        evidence=[evidence(doc)] if status in ("favorable", "unfavorable", "ambiguous") else [],
    )


def agg(
    signal_id: str, status: str, severity: Severity | None = None, conflict: bool = False
) -> SignalAggregate:
    spec = TAXONOMY.by_id()[signal_id]
    return SignalAggregate(
        signal_id=signal_id,
        dimension=spec.dimension,
        status=status,
        severity=severity,
        conflict=conflict,
    )  # type: ignore[arg-type]


class TestMappingContractFindings:
    spec = TAXONOMY.by_id()["LEG-LIAB-CAP"]
    two = TAXONOMY.by_id()["LEG-INDEMNITY"]  # IND-001 and IND-002

    def fact(
        self, rule: str, status: ClauseStatus, severity: Severity = Severity.INFO
    ) -> FindingFacts:
        return FindingFacts(rule, status, severity, [evidence()])

    @pytest.mark.parametrize(
        ("status", "severity", "expected", "carried"),
        [
            (ClauseStatus.ACCEPTABLE, Severity.INFO, "favorable", None),
            (ClauseStatus.PROBLEMATIC, Severity.CRITICAL, "unfavorable", Severity.CRITICAL),
            (ClauseStatus.PROBLEMATIC, Severity.LOW, "unfavorable", Severity.LOW),
            (ClauseStatus.PROBLEMATIC, Severity.INFO, "unfavorable", Severity.LOW),
            (ClauseStatus.AMBIGUOUS, Severity.MEDIUM, "ambiguous", None),
            (ClauseStatus.ABSENT, Severity.HIGH, "absent", None),
            (ClauseStatus.INSUFFICIENT_EVIDENCE, Severity.INFO, "insufficient_evidence", None),
        ],
    )
    def test_the_spec_table(
        self, status: ClauseStatus, severity: Severity, expected: str, carried: Severity | None
    ) -> None:
        o = observe_document(
            self.spec, DOC_A, [self.fact("LIAB-001", status, severity)], analysis_failed=False
        )
        assert (o.status, o.severity) == (expected, carried)
        assert bool(o.evidence) == (expected in ("favorable", "unfavorable", "ambiguous"))

    def test_a_signal_over_two_rules_takes_the_riskier_reading(self) -> None:
        facts = [
            self.fact("IND-001", ClauseStatus.ACCEPTABLE),
            self.fact("IND-002", ClauseStatus.PROBLEMATIC, Severity.HIGH),
        ]
        o = observe_document(self.two, DOC_A, facts, analysis_failed=False)
        assert (o.status, o.severity) == ("unfavorable", Severity.HIGH)

    def test_one_rule_absent_and_the_other_unknown_is_not_absent(self) -> None:
        facts = [
            self.fact("IND-001", ClauseStatus.ABSENT),
            self.fact("IND-002", ClauseStatus.INSUFFICIENT_EVIDENCE),
        ]
        assert (
            observe_document(self.two, DOC_A, facts, analysis_failed=False).status
            == "insufficient_evidence"
        )

    def test_every_rule_absent_is_absent_and_a_reading_beats_an_absence(self) -> None:
        both = [
            self.fact("IND-001", ClauseStatus.ABSENT),
            self.fact("IND-002", ClauseStatus.ABSENT),
        ]
        assert observe_document(self.two, DOC_A, both, analysis_failed=False).status == "absent"
        mixed = [
            self.fact("IND-001", ClauseStatus.ABSENT),
            self.fact("IND-002", ClauseStatus.ACCEPTABLE),
        ]
        assert observe_document(self.two, DOC_A, mixed, analysis_failed=False).status == "favorable"

    def test_no_finding_or_a_failed_analysis_is_insufficient_with_a_note(self) -> None:
        assert (
            observe_document(self.spec, DOC_A, [], analysis_failed=False).status
            == "insufficient_evidence"
        )
        failed = observe_document(
            self.spec, DOC_A, [self.fact("LIAB-001", ClauseStatus.ACCEPTABLE)], analysis_failed=True
        )
        assert failed.status == "insufficient_evidence" and "failed" in (failed.note or "")

    def test_only_contract_signals_are_mapped_and_for_every_document(self) -> None:
        mapped = map_contract_signals(SPECS, [DOC_A, DOC_B], {}, failed_documents=[DOC_B])
        contract_signals = [s for s in SPECS if s.from_contract]
        assert len(mapped) == 2 * len(contract_signals)
        assert {o.signal_id for o in mapped} == {s.id for s in contract_signals}
        assert all(o.status == "insufficient_evidence" for o in mapped)


class TestAggregation:
    spec = TAXONOMY.by_id()["LEG-LIAB-CAP"]

    def test_the_most_risky_evidence_based_observation_wins(self) -> None:
        a = aggregate_signal(
            self.spec,
            [obs("LEG-LIAB-CAP", "favorable", DOC_A), obs("LEG-LIAB-CAP", "ambiguous", DOC_B)],
        )
        assert a.status == "ambiguous" and not a.conflict
        b = aggregate_signal(
            self.spec,
            [
                obs("LEG-LIAB-CAP", "ambiguous", DOC_A),
                obs("LEG-LIAB-CAP", "unfavorable", DOC_B, Severity.MEDIUM),
            ],
        )
        assert (b.status, b.severity) == ("unfavorable", Severity.MEDIUM)

    def test_unfavorable_severities_are_ordered(self) -> None:
        a = aggregate_signal(
            self.spec,
            [
                obs("LEG-LIAB-CAP", "unfavorable", DOC_A, Severity.LOW),
                obs("LEG-LIAB-CAP", "unfavorable", DOC_B, Severity.CRITICAL),
            ],
        )
        assert a.severity is Severity.CRITICAL

    def test_favorable_against_unfavorable_is_a_conflict_and_scored_conservatively(self) -> None:
        a = aggregate_signal(
            self.spec,
            [
                obs("LEG-LIAB-CAP", "favorable", DOC_A),
                obs("LEG-LIAB-CAP", "unfavorable", DOC_B, Severity.HIGH),
            ],
        )
        assert a.conflict and a.status == "unfavorable"

    def test_an_evidence_based_reading_beats_absences_and_unknowns(self) -> None:
        a = aggregate_signal(
            self.spec,
            [
                obs("LEG-LIAB-CAP", "absent", DOC_A),
                obs("LEG-LIAB-CAP", "insufficient_evidence", DOC_B),
                obs("LEG-LIAB-CAP", "favorable", DOC_C),
            ],
        )
        assert a.status == "favorable"

    def test_without_evidence_it_is_absent_only_if_every_document_says_so(self) -> None:
        assert (
            aggregate_signal(
                self.spec,
                [obs("LEG-LIAB-CAP", "absent", DOC_A), obs("LEG-LIAB-CAP", "absent", DOC_B)],
            ).status
            == "absent"
        )
        assert (
            aggregate_signal(
                self.spec,
                [
                    obs("LEG-LIAB-CAP", "absent", DOC_A),
                    obs("LEG-LIAB-CAP", "insufficient_evidence", DOC_B),
                ],
            ).status
            == "insufficient_evidence"
        )
        assert aggregate_signal(self.spec, []).status == "insufficient_evidence"

    def test_the_observations_are_kept_for_drill_down(self) -> None:
        a = aggregate_signal(
            self.spec,
            [obs("LEG-LIAB-CAP", "favorable", DOC_B), obs("LEG-LIAB-CAP", "ambiguous", DOC_A)],
        )
        assert [o.document_id for o in a.observations] == [DOC_A, DOC_B]

    @given(
        st.permutations(
            [
                ("favorable", None),
                ("ambiguous", None),
                ("unfavorable", Severity.HIGH),
                ("absent", None),
                ("insufficient_evidence", None),
            ]
        )
    )
    def test_the_order_of_the_documents_does_not_matter(
        self, ordering: list[tuple[str, Severity | None]]
    ) -> None:
        docs = [UUID(int=100 + i) for i in range(len(ordering))]
        observations = [
            obs("LEG-LIAB-CAP", s, d, sev) for (s, sev), d in zip(ordering, docs, strict=True)
        ]
        baseline = aggregate_signal(
            self.spec, sorted(observations, key=lambda o: str(o.document_id))
        )
        assert aggregate_signal(self.spec, observations).model_dump() == baseline.model_dump()


# ---- scoring ------------------------------------------------------------------------------------

STATUSES = [
    "favorable",
    "ambiguous",
    "unfav-low",
    "unfav-medium",
    "unfav-high",
    "unfav-critical",
    "absent",
    "insufficient_evidence",
]


def to_aggregate(signal_id: str, label: str) -> SignalAggregate:
    if label.startswith("unfav-"):
        return agg(signal_id, "unfavorable", Severity(label.split("-")[1]))
    return agg(signal_id, label)


assignments = st.fixed_dictionaries({s.id: st.sampled_from(STATUSES) for s in SPECS})


def build(assignment: dict[str, str]) -> dict[str, SignalAggregate]:
    return {sid: to_aggregate(sid, label) for sid, label in assignment.items()}


RELAXED = settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])


class TestScoringExamples:
    def test_a_dimension_of_two_signals(self) -> None:
        # DS: weights DPA 3, breach 3, subproc 2, sec-std 2, pen-test 1 = 11
        aggregates = {
            "DS-DPA": agg("DS-DPA", "favorable"),  # 10, weight 3
            "DS-BREACH-NOTICE": agg("DS-BREACH-NOTICE", "ambiguous"),  # 50, weight 3
            "DS-SUBPROC": agg("DS-SUBPROC", "unfavorable", Severity.HIGH),  # 80, weight 2
        }
        result = score_dimension(
            SCORING,
            RiskDimension.DATA_SECURITY,
            TAXONOMY.for_dimension(RiskDimension.DATA_SECURITY),
            aggregates,
        )
        assert result.score == pytest.approx((3 * 10 + 3 * 50 + 2 * 80) / 8)
        assert result.coverage == pytest.approx(8 / 11, abs=1e-3)
        assert result.status is DimensionStatus.ASSESSED
        reasons = {m.signal_id: m.reason for m in result.missing_information}
        assert reasons == {
            "DS-SEC-STD": "insufficient_evidence",
            "DS-PEN-TEST": "insufficient_evidence",
        }

    def test_below_minimum_coverage_the_score_is_shown_but_flagged(self) -> None:
        aggregates = {"DS-DPA": agg("DS-DPA", "favorable")}  # 3 of 11
        result = score_dimension(
            SCORING,
            RiskDimension.DATA_SECURITY,
            TAXONOMY.for_dimension(RiskDimension.DATA_SECURITY),
            aggregates,
        )
        assert result.status is DimensionStatus.INSUFFICIENT_EVIDENCE and result.score == 10

    def test_with_no_included_signal_there_is_no_score(self) -> None:
        result = score_dimension(
            SCORING, RiskDimension.COMPLIANCE, TAXONOMY.for_dimension(RiskDimension.COMPLIANCE), {}
        )
        assert (
            result.status is DimensionStatus.NOT_ASSESSED
            and result.score is None
            and result.coverage == 0
        )
        assert result.severity is Severity.INFO and len(result.missing_information) == 3

    def test_a_plain_absence_is_a_gap_not_a_score(self) -> None:
        scored = score_signal(SCORING, TAXONOMY.by_id()["OPS-SLA"], agg("OPS-SLA", "absent"))
        assert (
            scored.risk is None
            and scored.gap == "absent_in_all_documents"
            and not scored.policy_treated
        )

    def test_a_documented_absence_counts_as_unfavorable_by_policy(self) -> None:
        scored = score_signal(
            SCORING, TAXONOMY.by_id()["LEG-LIAB-CAP"], agg("LEG-LIAB-CAP", "absent")
        )
        assert (scored.status, scored.severity, scored.risk, scored.policy_treated) == (
            "unfavorable",
            Severity.HIGH,
            80,
            True,
        )
        exit_signal = score_signal(SCORING, TAXONOMY.by_id()["DEP-EXIT"], agg("DEP-EXIT", "absent"))
        assert exit_signal.risk == 55  # medium

    def test_insufficient_evidence_is_a_gap_even_where_absence_is_policy(self) -> None:
        scored = score_signal(
            SCORING, TAXONOMY.by_id()["LEG-LIAB-CAP"], agg("LEG-LIAB-CAP", "insufficient_evidence")
        )
        assert scored.risk is None and scored.gap == "insufficient_evidence"

    def test_an_unaggregated_signal_is_a_gap(self) -> None:
        assert (
            score_signal(SCORING, TAXONOMY.by_id()["OPS-SLA"], None).gap == "insufficient_evidence"
        )

    def full(self, label: str) -> dict[str, SignalAggregate]:
        return build({s.id: label for s in SPECS})

    def test_everything_favorable(self) -> None:
        result = score_assessment(SCORING, TAXONOMY, self.full("favorable"))
        assert result.overall_score == 10 and result.overall_severity is Severity.LOW
        assert result.completeness == pytest.approx(1.0) and result.evidence_coverage == 1.0
        assert not result.provisional and len(result.assessed_dimensions) == 6

    def test_nothing_known_is_provisional_with_no_overall_score(self) -> None:
        result = score_assessment(SCORING, TAXONOMY, {})
        assert (
            result.overall_score is None
            and result.provisional
            and result.overall_severity is Severity.INFO
        )
        assert (
            result.completeness == 0
            and result.evidence_coverage == 0
            and result.assessed_dimensions == []
        )

    def test_the_overall_score_renormalises_over_the_assessed_dimensions(self) -> None:
        aggregates = build(
            {
                s.id: "favorable"
                for s in SPECS
                if s.dimension in (RiskDimension.LEGAL, RiskDimension.DATA_SECURITY)
            }
        )
        for s in TAXONOMY.for_dimension(RiskDimension.OPERATIONAL):
            aggregates[s.id] = agg(s.id, "unfavorable", Severity.CRITICAL)
        result = score_assessment(SCORING, TAXONOMY, aggregates)
        assert set(result.assessed_dimensions) == {
            RiskDimension.LEGAL,
            RiskDimension.DATA_SECURITY,
            RiskDimension.OPERATIONAL,
        }
        # weights 0.25, 0.25, 0.20 over 0.70
        assert result.overall_score == pytest.approx(
            (0.25 * 10 + 0.25 * 10 + 0.20 * 100) / 0.70, abs=1e-3
        )
        weights = effective_weights(SCORING, result.assessed_dimensions)
        assert (
            sum(weights.values()) == pytest.approx(1.0) and RiskDimension.COMPLIANCE not in weights
        )

    def test_fewer_than_four_assessed_dimensions_is_provisional_whatever_the_completeness(
        self,
    ) -> None:
        three = [RiskDimension.LEGAL, RiskDimension.DATA_SECURITY, RiskDimension.OPERATIONAL]
        result = score_assessment(
            SCORING, TAXONOMY, build({s.id: "favorable" for s in SPECS if s.dimension in three})
        )
        assert (
            result.completeness > SCORING.provisional_completeness_threshold and result.provisional
        )

    def test_low_completeness_is_provisional_even_with_four_dimensions(self) -> None:
        four = [
            RiskDimension.OPERATIONAL,
            RiskDimension.FINANCIAL_COMMERCIAL,
            RiskDimension.COMPLIANCE,
            RiskDimension.DEPENDENCY,
        ]  # together 0.50 of the weight
        result = score_assessment(
            SCORING, TAXONOMY, build({s.id: "favorable" for s in SPECS if s.dimension in four})
        )
        assert (
            len(result.assessed_dimensions) == 4
            and result.completeness < 0.6
            and result.provisional
        )


class TestScoringProperties:
    @RELAXED
    @given(assignments)
    def test_scores_stay_between_zero_and_a_hundred_and_between_the_extremes_used(
        self, assignment: dict[str, str]
    ) -> None:
        result = score_assessment(SCORING, TAXONOMY, build(assignment))
        for d in result.dimensions:
            if d.score is not None:
                risks = [s.risk for s in d.signals if s.risk is not None]
                assert 0 <= d.score <= 100 and min(risks) - 1e-9 <= d.score <= max(risks) + 1e-9
            assert 0 <= d.coverage <= 1
        assert 0 <= result.completeness <= 1 and 0 <= result.evidence_coverage <= 1
        assert (result.overall_score is None) == (not result.assessed_dimensions)

    @RELAXED
    @given(assignments, st.data())
    def test_a_favorable_signal_never_raises_a_dimension_score(
        self, assignment: dict[str, str], data: st.DataObject
    ) -> None:
        gaps = [
            sid
            for sid, label in assignment.items()
            if label in ("absent", "insufficient_evidence")
            and sid not in SCORING.absent_policy.overrides
        ]
        if not gaps:
            return
        target = data.draw(st.sampled_from(gaps))
        before = score_assessment(SCORING, TAXONOMY, build(assignment))
        after = score_assessment(SCORING, TAXONOMY, build({**assignment, target: "favorable"}))
        dimension = TAXONOMY.by_id()[target].dimension
        b = next(d for d in before.dimensions if d.dimension is dimension)
        a = next(d for d in after.dimensions if d.dimension is dimension)
        if b.score is not None:
            assert a.score is not None and a.score <= b.score + 1e-9
        assert a.coverage >= b.coverage

    @RELAXED
    @given(assignments, st.data())
    def test_a_riskier_status_never_lowers_a_dimension_score(
        self, assignment: dict[str, str], data: st.DataObject
    ) -> None:
        # ordered by the risk the configuration assigns (10, 30, 50, 55, 80, 100): an
        # unfavorable-low signal is less risky than an ambiguous one in the shipped numbers
        ladder = [
            "favorable",
            "unfav-low",
            "ambiguous",
            "unfav-medium",
            "unfav-high",
            "unfav-critical",
        ]
        candidates = [sid for sid, label in assignment.items() if label in ladder[:-1]]
        if not candidates:
            return
        target = data.draw(st.sampled_from(candidates))
        step = data.draw(st.integers(1, len(ladder) - 1 - ladder.index(assignment[target])))
        riskier = ladder[ladder.index(assignment[target]) + step]
        dimension = TAXONOMY.by_id()[target].dimension

        def score(a: dict[str, str]) -> float | None:
            result = score_assessment(SCORING, TAXONOMY, build(a))
            return next(d for d in result.dimensions if d.dimension is dimension).score

        assert (score({**assignment, target: riskier}) or 0) >= (score(assignment) or 0) - 1e-9

    @RELAXED
    @given(assignments, st.integers(1, 4), st.lists(st.floats(0.5, 5.0), min_size=1, max_size=3))
    def test_gaps_never_change_a_score_they_only_lower_coverage(
        self, assignment: dict[str, str], dimension_index: int, weights: list[float]
    ) -> None:
        dimension = list(RiskDimension)[dimension_index]
        extra = [
            SignalSpec.model_validate(
                {
                    "id": f"XX-GAP{i}",
                    "dimension": dimension.value,
                    "label": "extra",
                    "weight": w,
                    "source": {"contract_rules": ["LIAB-001"]},
                }
            )
            for i, w in enumerate(weights)
        ]
        bigger = SignalsConfig(
            version=TAXONOMY.version, signals=[*TAXONOMY.signals, *extra]
        )  # the extras have no aggregate: gaps
        aggregates = build(assignment)
        before = next(
            d
            for d in score_assessment(SCORING, TAXONOMY, aggregates).dimensions
            if d.dimension is dimension
        )
        after = next(
            d
            for d in score_assessment(SCORING, bigger, aggregates).dimensions
            if d.dimension is dimension
        )
        assert after.score == before.score
        assert after.coverage <= before.coverage + 1e-9

    @RELAXED
    @given(assignments)
    def test_identical_inputs_give_identical_outputs(self, assignment: dict[str, str]) -> None:
        first = score_assessment(SCORING, TAXONOMY, build(assignment)).model_dump()
        assert score_assessment(SCORING, TAXONOMY, build(assignment)).model_dump() == first

    @RELAXED
    @given(
        st.lists(
            st.tuples(
                st.sampled_from(
                    ["favorable", "ambiguous", "unfavorable", "absent", "insufficient_evidence"]
                ),
                st.sampled_from(list(Severity)[1:]),
            ),
            min_size=1,
            max_size=5,
        ),
        st.randoms(),
    )
    def test_a_permutation_of_the_documents_does_not_change_the_result(
        self, readings: list[tuple[str, Severity]], rng: random.Random
    ) -> None:
        observations = [
            obs(spec.id, status, UUID(int=500 + i), severity if status == "unfavorable" else None)
            for spec in SPECS
            for i, (status, severity) in enumerate(readings)
        ]
        shuffled = list(observations)
        rng.shuffle(shuffled)
        one = score_assessment(SCORING, TAXONOMY, aggregate_all(SPECS, observations)).model_dump()
        two = score_assessment(SCORING, TAXONOMY, aggregate_all(SPECS, shuffled)).model_dump()
        assert one == two

    @RELAXED
    @given(assignments)
    def test_effective_weights_sum_to_one_over_what_was_assessed(
        self, assignment: dict[str, str]
    ) -> None:
        result = score_assessment(SCORING, TAXONOMY, build(assignment))
        weights = effective_weights(SCORING, result.assessed_dimensions)
        if result.assessed_dimensions:
            assert sum(weights.values()) == pytest.approx(1.0)
        else:
            assert weights == {}
