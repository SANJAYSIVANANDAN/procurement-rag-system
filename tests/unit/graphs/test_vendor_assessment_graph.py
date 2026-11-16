"""The vendor assessment graph end to end on fakes."""

from typing import Any
from uuid import uuid4

import pytest

from covenantiq.ai.llm.base import LLMUnavailable
from covenantiq.ai.schemas import DimensionExplanation, Mitigation, SignalExtraction
from covenantiq.core.enums import ClauseStatus, DimensionStatus, RiskDimension, Severity
from covenantiq.core.errors import PermanentTaskError
from covenantiq.graphs.vendor_assessment.deps import DocumentAnalysis
from tests.unit.graphs.analysis_harness import Crash
from tests.unit.graphs.vendor_harness import (
    DOC_A,
    DOC_B,
    Vendor,
    favorable,
    findings,
    two_documents,
)


class TestAnAssessment:
    async def test_a_clean_vendor_is_assessed_in_all_six_dimensions(self) -> None:
        v = Vendor()
        await v.run()
        record = v.record
        assert record.score.overall_score == 10 and not record.score.provisional
        assert len(record.score.assessed_dimensions) == 6
        assert record.score.evidence_coverage == 1.0
        assert record.scoring_config_version == "vendor_scoring_v1@1.0.0"
        assert [d["title"] for d in record.documents] == ["Acme MSA"]
        assert set(record.weights) == {d.value for d in RiskDimension}

    async def test_every_dimension_carries_signals_reasoning_and_evidence(self) -> None:
        v = Vendor()
        await v.run()
        for dimension in v.record.score.dimensions:
            name = dimension.dimension.value
            assert v.record.reasoning[name] and v.record.dimension_signals[name]
            assert v.record.evidence[name], name
        row = v.record.dimension_signals["legal"][0]
        assert (row["signal_id"], row["weight"]) == ("LEG-INDEMNITY", 3)  # heaviest first, then id
        assert all("observations" in r for r in v.record.dimension_signals["legal"])

    async def test_the_assessment_is_persisted_once_and_progress_ends_at_one(self) -> None:
        v = Vendor()
        await v.run()
        assert v.store.persist_calls == 1
        fractions = [f for _, f in v.store.progress]
        assert fractions == sorted(fractions) and fractions[-1] == 1.0

    async def test_the_checkpoints_are_removed_afterwards(self) -> None:
        v = Vendor()
        await v.run()
        config: Any = {"configurable": {"thread_id": str(v.job_id)}}
        assert await v.saver.aget_tuple(config) is None


class TestContractSignals:
    async def test_findings_become_signal_statuses(self) -> None:
        facts = findings(
            ClauseStatus.ACCEPTABLE,
            overrides={
                "LIAB-002": ClauseStatus.PROBLEMATIC,
                "GOV-001": ClauseStatus.AMBIGUOUS,
                "SLA-001": ClauseStatus.INSUFFICIENT_EVIDENCE,
            },
        )
        v = Vendor(
            analyses=[
                DocumentAnalysis(document_id=DOC_A, state="current", job_id=uuid4(), findings=facts)
            ]
        )
        await v.run()
        assert (v.signal("LEG-CONSEQ").status, v.signal("LEG-CONSEQ").severity) == (
            "unfavorable",
            Severity.HIGH,
        )
        assert v.signal("LEG-GOV-LAW").status == "ambiguous"
        assert (
            v.signal("OPS-SLA").gap == "insufficient_evidence" and v.signal("OPS-SLA").risk is None
        )

    async def test_a_documented_absence_counts_as_risk_and_a_plain_one_is_a_gap(self) -> None:
        facts = findings(
            ClauseStatus.ACCEPTABLE,
            overrides={"LIAB-001": ClauseStatus.ABSENT, "SLA-001": ClauseStatus.ABSENT},
        )
        v = Vendor(
            analyses=[
                DocumentAnalysis(document_id=DOC_A, state="current", job_id=uuid4(), findings=facts)
            ]
        )
        await v.run()
        cap = v.signal("LEG-LIAB-CAP")
        assert (cap.status, cap.policy_treated, cap.risk) == ("unfavorable", True, 80)
        sla = v.signal("OPS-SLA")
        assert sla.gap == "absent_in_all_documents" and sla.risk is None
        missing = {m.signal_id for m in v.dimension("operational").missing_information}
        assert "OPS-SLA" in missing

    async def test_across_documents_the_riskier_reading_wins_and_a_conflict_is_flagged(
        self,
    ) -> None:
        bad = findings(
            ClauseStatus.ACCEPTABLE, doc=DOC_B, overrides={"LIAB-001": ClauseStatus.PROBLEMATIC}
        )
        v = Vendor(
            scope=two_documents(),
            analyses=[
                DocumentAnalysis(
                    document_id=DOC_A,
                    state="current",
                    job_id=uuid4(),
                    findings=findings(ClauseStatus.ACCEPTABLE),
                ),
                DocumentAnalysis(document_id=DOC_B, state="current", job_id=uuid4(), findings=bad),
            ],
        )
        await v.run()
        cap = v.signal("LEG-LIAB-CAP")
        assert cap.status == "unfavorable" and cap.conflict
        assert set(cap.document_ids) == {DOC_A, DOC_B}
        assert [d["title"] for d in v.record.documents] == ["Acme MSA", "Acme DPA"]

    async def test_a_failed_contract_analysis_makes_that_documents_signals_insufficient_and_says_so(
        self,
    ) -> None:
        v = Vendor(
            scope=two_documents(),
            analyses=[
                DocumentAnalysis(
                    document_id=DOC_A,
                    state="current",
                    job_id=uuid4(),
                    findings=findings(ClauseStatus.ACCEPTABLE),
                ),
                DocumentAnalysis(document_id=DOC_B, state="failed", job_id=uuid4()),
            ],
        )
        await v.run()
        assert v.signal("LEG-LIAB-CAP").status == "favorable"  # document A still speaks
        assert any("Acme DPA" in n and "failed" in n for n in v.record.notes)

    async def test_a_document_without_a_finished_analysis_makes_the_run_fail_retryably(
        self,
    ) -> None:
        v = Vendor(analyses=[DocumentAnalysis(document_id=DOC_A, state="pending", job_id=uuid4())])
        with pytest.raises(PermanentTaskError) as caught:
            await v.run()
        assert caught.value.job_code == "DEPENDENCY_NOT_READY" and v.store.persist_calls == 0
        assert v.llm.calls == 0


class TestExtractedSignals:
    async def test_the_extraction_sourced_signals_are_read_by_the_model_with_evidence(self) -> None:
        v = Vendor()
        await v.run()
        pen = v.signal("DS-PEN-TEST")
        assert pen.status == "favorable" and pen.risk == 10
        assert v.record.evidence["data_security"]

    async def test_each_dimension_with_extraction_signals_costs_one_call(self) -> None:
        v = Vendor()
        await v.run()
        # data security, operational, financial, compliance and dependency; legal has none
        assert v.extraction_calls == 5

    async def test_an_absence_is_refused_when_the_search_found_related_passages(self) -> None:
        def says_absent(prompt: str, signal_id: str) -> SignalExtraction:
            return SignalExtraction(signal_id=signal_id, status="absent", rationale="Nothing.")

        v = Vendor(extract=says_absent)  # the retriever returns a strong passage for every query
        await v.run()
        assert v.signal("DS-PEN-TEST").gap == "insufficient_evidence"

    async def test_an_absence_is_accepted_when_the_search_found_nothing(self) -> None:
        def says_absent(prompt: str, signal_id: str) -> SignalExtraction:
            return SignalExtraction(signal_id=signal_id, status="absent", rationale="Nothing.")

        v = Vendor(extract=says_absent)
        v.retriever.empty_for = ["penetration test", "independent security audit"]
        await v.run()
        assert v.signal("DS-PEN-TEST").gap == "absent_in_all_documents"

    async def test_an_absence_is_never_claimed_when_a_documents_text_was_incomplete(self) -> None:
        scope = two_documents()
        scope.documents[1] = scope.documents[1].model_copy(update={"extraction_ok": False})
        analyses = [
            DocumentAnalysis(
                document_id=d.id,
                state="current",
                job_id=uuid4(),
                findings=findings(ClauseStatus.ACCEPTABLE, doc=d.id),
            )
            for d in scope.documents
        ]

        def says_absent(prompt: str, signal_id: str) -> SignalExtraction:
            return SignalExtraction(signal_id=signal_id, status="absent", rationale="Nothing.")

        v = Vendor(scope=scope, analyses=analyses, extract=says_absent)
        v.retriever.empty_for = ["penetration test", "independent security audit"]
        await v.run()
        assert v.signal("DS-PEN-TEST").gap == "insufficient_evidence"

    async def test_a_verdict_citing_no_given_passage_is_insufficient(self) -> None:
        def bad_marker(prompt: str, signal_id: str) -> SignalExtraction:
            return favorable(prompt, signal_id).model_copy(update={"supporting_markers": [9]})

        v = Vendor(extract=bad_marker)
        await v.run()
        assert v.signal("DS-PEN-TEST").gap == "insufficient_evidence"

    async def test_a_rationale_the_passage_does_not_support_is_insufficient(self) -> None:
        def invented(prompt: str, signal_id: str) -> SignalExtraction:
            return favorable(prompt, signal_id).model_copy(
                update={"rationale": "The vendor guarantees ninety days of free consulting."}
            )

        v = Vendor(extract=invented)
        await v.run()
        assert v.signal("OPS-BCDR").gap == "insufficient_evidence"

    async def test_an_unfavorable_reading_carries_its_severity(self) -> None:
        def bad(prompt: str, signal_id: str) -> SignalExtraction:
            return favorable(prompt, signal_id).model_copy(
                update={"status": "unfavorable", "unfavorable_severity": "critical"}
            )

        v = Vendor(extract=bad)
        await v.run()
        pen = v.signal("DS-PEN-TEST")
        assert (pen.status, pen.severity, pen.risk) == ("unfavorable", Severity.CRITICAL, 100)

    async def test_a_failed_extraction_call_costs_that_dimension_only(self) -> None:
        def down_for_compliance(ids: list[str]) -> None:
            if "CMP-REGS" in ids:
                raise LLMUnavailable("down")

        v = Vendor(on_extraction=down_for_compliance)
        await v.run()
        assert v.signal("CMP-REGS").gap == "insufficient_evidence"
        assert v.signal("DS-PEN-TEST").status == "favorable"
        assert any("signal extraction failed" in n for n in v.record.notes)

    async def test_a_retrieval_failure_costs_those_signals_only(self) -> None:
        v = Vendor()
        v.retriever.fail_for = ["penetration test"]
        await v.run()
        assert v.signal("DS-PEN-TEST").gap == "insufficient_evidence"
        assert v.signal("OPS-SUPPORT").status == "favorable"


class TestScopeErrors:
    async def test_an_unknown_vendor(self) -> None:
        v = Vendor()
        v.store.scope = None
        with pytest.raises(PermanentTaskError) as caught:
            await v.run()
        assert caught.value.job_code == "VENDOR_NOT_FOUND"

    async def test_a_vendor_with_no_ready_documents(self) -> None:
        v = Vendor()
        assert v.scope is not None
        v.store.scope = v.scope.model_copy(update={"documents": []})
        with pytest.raises(PermanentTaskError) as caught:
            await v.run()
        assert caught.value.job_code == "NO_DOCUMENTS"


class TestExplanations:
    async def test_a_sound_explanation_and_its_valid_mitigations_are_kept(self) -> None:
        facts = findings(ClauseStatus.ACCEPTABLE, overrides={"DP-002": ClauseStatus.PROBLEMATIC})
        v = Vendor(
            analyses=[
                DocumentAnalysis(document_id=DOC_A, state="current", job_id=uuid4(), findings=facts)
            ]
        )
        original = v.llm._handlers["explain"]

        def explain(request: Any) -> Any:
            if 'name="data_security"' in request.messages[0].content:
                return DimensionExplanation(
                    reasoning="DS-BREACH-NOTICE is unfavorable and drives the data security score.",
                    mitigations=[
                        Mitigation(
                            text="Negotiate a 72-hour breach notice.",
                            signal_ids=["DS-BREACH-NOTICE"],
                        ),
                        Mitigation(
                            text="Praise the DPA.", signal_ids=["DS-DPA"]
                        ),  # favorable: dropped
                        Mitigation(
                            text="Do something vague.", signal_ids=[]
                        ),  # addresses nothing: dropped
                        Mitigation(
                            text="Fix a signal that does not exist.", signal_ids=["XX-NOPE"]
                        ),  # dropped
                    ],
                )
            return original(request)

        v.llm.script("explain", explain)
        await v.run()
        assert "DS-BREACH-NOTICE is unfavorable" in v.record.reasoning["data_security"]
        assert v.record.mitigations["data_security"] == ["Negotiate a 72-hour breach notice."]

    @pytest.mark.parametrize(
        ("reasoning", "why"),
        [
            ("OPS-SLA is excellent and XYZ-MAGIC is too.", "unknown signal"),
            ("The data security score is 99 which is excellent.", "number that was not computed"),
            ("", "empty"),
        ],
    )
    async def test_an_explanation_that_does_not_check_out_is_replaced_by_the_template(
        self, reasoning: str, why: str
    ) -> None:
        v = Vendor()

        def bad(request: Any) -> Any:
            return DimensionExplanation(
                reasoning=reasoning or " ",
                mitigations=[Mitigation(text="x", signal_ids=["DS-DPA"])],
            )

        v.llm.script("explain", bad)
        await v.run()
        text = v.record.reasoning["data_security"]
        assert text.startswith("The data security risk score is 10") and "XYZ" not in text
        assert v.record.mitigations["data_security"] == []
        assert any("fell back to the template" in n and why in n for n in v.record.notes)

    async def test_an_unavailable_model_gives_template_explanations_and_the_run_still_succeeds(
        self,
    ) -> None:
        v = Vendor()

        def down(request: Any) -> Any:
            raise LLMUnavailable("down")

        v.llm.script("explain", down)
        await v.run()
        assert all(text for text in v.record.reasoning.values())
        assert v.store.persist_calls == 1

    async def test_the_template_names_the_unfavorable_and_the_unassessed_signals(self) -> None:
        facts = findings(
            ClauseStatus.ACCEPTABLE,
            overrides={
                "DP-002": ClauseStatus.PROBLEMATIC,
                "SLA-001": ClauseStatus.INSUFFICIENT_EVIDENCE,
            },
        )
        v = Vendor(
            analyses=[
                DocumentAnalysis(document_id=DOC_A, state="current", job_id=uuid4(), findings=facts)
            ]
        )
        down = lambda request: (_ for _ in ()).throw(LLMUnavailable("down"))  # noqa: E731
        v.llm.script("explain", down)
        await v.run()
        assert "Unfavorable: DS-BREACH-NOTICE." in v.record.reasoning["data_security"]
        assert "Not assessed for lack of evidence: OPS-SLA" in v.record.reasoning["operational"]

    async def test_a_dimension_with_nothing_is_not_assessed_and_the_template_says_so(self) -> None:
        v = Vendor(analyses=[DocumentAnalysis(document_id=DOC_A, state="failed", job_id=uuid4())])

        def nothing(prompt: str, signal_id: str) -> SignalExtraction:
            return SignalExtraction(
                signal_id=signal_id, status="insufficient_evidence", rationale="Cannot tell."
            )

        v.extract = nothing
        v.llm.script("explain", lambda r: (_ for _ in ()).throw(LLMUnavailable("down")))
        await v.run()
        legal = v.dimension("legal")
        assert legal.status is DimensionStatus.NOT_ASSESSED and legal.score is None
        assert len(legal.missing_information) == 4
        assert v.record.reasoning["legal"] == (
            "The legal dimension could not be assessed: no signal had usable evidence."
        )
        assert v.record.score.overall_score is None and v.record.score.provisional


class TestTheModelNeverScores:
    def test_no_vendor_output_schema_has_a_numeric_field_except_source_numbers(self) -> None:
        for schema in (SignalExtraction, DimensionExplanation):
            props = schema.model_json_schema().get("properties", {})
            for name, spec in props.items():
                kinds = {
                    spec.get("type"),
                    *(
                        i.get("type")
                        for i in spec.get("items", {}).get("anyOf", [spec.get("items", {})])
                    ),
                }
                numeric = kinds & {"number", "integer"}
                assert not numeric or name == "supporting_markers", (schema.__name__, name)

    async def test_a_models_attempt_to_change_a_score_is_ignored(self) -> None:
        v = Vendor()
        v.llm.script(
            "explain", lambda r: DimensionExplanation(reasoning="The risk is 3.", mitigations=[])
        )
        await v.run()
        assert v.record.score.overall_score == 10  # the computed score stands
        assert "risk score is 10" in v.record.reasoning["legal"]


class TestResume:
    async def test_a_crash_after_the_extraction_resumes_without_asking_the_model_again(
        self,
    ) -> None:
        v = Vendor()
        state = {"crashed": False}

        def crash_once(request: Any) -> Any:
            if not state["crashed"]:
                state["crashed"] = True
                raise Crash()
            return DimensionExplanation(reasoning="A summary of the dimension.", mitigations=[])

        v.llm.script("explain", crash_once)
        with pytest.raises(Crash):
            await v.run()
        calls = v.extraction_calls
        assert calls == 5 and v.store.persist_calls == 0

        outcome = await v.run()

        assert outcome.resumed and v.store.persist_calls == 1
        assert v.extraction_calls == calls  # nothing was read twice
