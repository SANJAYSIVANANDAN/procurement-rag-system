"""The contract analysis graph end to end on fakes: fan-out, every status, degradation, resume."""

import math
from typing import Any

import pytest

from covenantiq.ai.llm.base import LLMUnavailable
from covenantiq.ai.schemas import ObligationExtraction, ObligationItem
from covenantiq.analysis.contract.types import RuleResult
from covenantiq.core.enums import (
    ClauseStatus,
    ContractType,
    EvidenceSufficiency,
    RiskCategory,
    Severity,
)
from covenantiq.core.errors import PermanentTaskError
from covenantiq.graphs.contract_analysis.deps import ChunkText
from covenantiq.graphs.contract_analysis.nodes import ContractNodes
from tests.unit.graphs.analysis_harness import (
    RULESET,
    Analysis,
    Crash,
    ready_document,
)


def applicable(kind: str) -> list[str]:
    return [r.id for r in RULESET.applicable(ContractType(kind))]


class TestARun:
    async def test_every_applicable_rule_gets_a_finding_in_batches_of_four(self) -> None:
        a = Analysis(ready_document())
        outcome = await a.run()

        rules = applicable("saas")
        assert outcome.findings == len(a.findings) >= len(rules)
        assert {f.rule_id for f in a.findings if "-" in f.rule_id and f.rule_id[0] != "O"} >= set(
            rules
        )
        assert len(a.batches_assessed) == math.ceil(len(rules) / 4)
        assert all(len(batch) <= 4 for batch in a.batches_assessed)
        assert sorted(i for b in a.batches_assessed for i in b) == sorted(rules)

    async def test_an_acceptable_clause_is_stored_as_info_with_its_evidence(self) -> None:
        a = Analysis(ready_document())
        await a.run()
        finding = a.by_rule("LIAB-001")
        assert (
            finding.clause_status is ClauseStatus.ACCEPTABLE and finding.severity is Severity.INFO
        )
        assert finding.evidence_sufficiency in (
            EvidenceSufficiency.MODERATE,
            EvidenceSufficiency.STRONG,
        )
        assert len(finding.evidence) == 1 and finding.evidence[0].document_title == "Acme MSA"
        assert finding.confidence > 0.5

    async def test_the_job_is_persisted_once_with_stats_and_usage(self) -> None:
        a = Analysis(ready_document())
        await a.run()
        assert a.store.persist_calls == 1
        stats = a.store.stats[a.job_id]
        assert stats["by_status"]["acceptable"] >= 20 and stats["usage"]["output_tokens"] >= 0

    async def test_progress_climbs_and_ends_at_one(self) -> None:
        a = Analysis(ready_document())
        await a.run()
        fractions = [f for _, f in a.store.progress]
        assert fractions == sorted(fractions) and fractions[-1] == 1.0
        assert {"assessing_rules", "obligations", "done"} <= {s for s, _ in a.store.progress}

    async def test_the_checkpoints_are_removed_when_the_job_succeeds(self) -> None:
        a = Analysis(ready_document())
        await a.run()
        config: Any = {"configurable": {"thread_id": str(a.job_id)}}
        assert await a.saver.aget_tuple(config) is None

    async def test_an_unset_contract_type_is_noted(self) -> None:
        a = Analysis(ready_document(contract_type="unknown"))
        await a.run()
        assert any("contract type was not set" in n for n in a.store.stats[a.job_id]["notes"])

    async def test_running_the_same_job_again_replaces_its_findings(self) -> None:
        """Whether a rerun can happen at all is the job claim's business (tested with the
        service); if it does, the findings are replaced, never doubled."""
        a = Analysis(ready_document())
        await a.run()
        first = [(f.rule_id, f.ordinal) for f in a.findings]
        await a.run()
        assert [(f.rule_id, f.ordinal) for f in a.findings] == first
        assert list(a.store.persisted) == [a.job_id]


class TestStatuses:
    async def test_a_missing_clause_is_absent_with_the_search_recorded(self) -> None:
        a = Analysis(ready_document())
        a.retriever.empty_for = ["service level", "service availability"]
        await a.run()
        finding = a.by_rule("SLA-001")
        assert finding.clause_status is ClauseStatus.ABSENT and finding.severity is Severity.MEDIUM
        assert finding.evidence == [] and finding.evidence_sufficiency is EvidenceSufficiency.NONE
        trace = finding.details["search_trace"]
        assert (
            trace["queries_run"] == 2 and trace["pages_scanned"] == 40 and trace["candidates"] == 0
        )
        assert "searched" in finding.explanation and "40 pages" in finding.explanation
        assert finding.recommended_action and finding.title == "No service levels found"

    async def test_a_tagged_clause_the_search_missed_blocks_the_claim_of_absence(self) -> None:
        a = Analysis(ready_document(clause_types_present=["service_levels"]))
        a.retriever.empty_for = ["service level", "service availability"]
        await a.run()
        assert a.by_rule("SLA-001").clause_status is ClauseStatus.INSUFFICIENT_EVIDENCE

    async def test_poor_extraction_means_nothing_is_called_absent_or_acceptable(self) -> None:
        doc = ready_document(
            extraction_quality={
                "pages_total": 40,
                "pages_with_text": 20,
                "insufficient_text": False,
            }
        )
        a = Analysis(doc)
        a.retriever.empty_for = ["service level", "service availability"]
        await a.run()
        statuses = {f.clause_status for f in a.findings if f.details.get("kind") == "rule"}
        assert statuses == {ClauseStatus.INSUFFICIENT_EVIDENCE}

    async def test_a_red_flag_pattern_forces_problematic_and_raises_severity(self) -> None:
        a = Analysis(ready_document())
        a.retriever.text_for["limitation of liability"] = (
            "The Supplier accepts unlimited liability for any loss."
        )
        await a.run()
        finding = a.by_rule("LIAB-001")
        assert (
            finding.clause_status is ClauseStatus.PROBLEMATIC
            and finding.severity is Severity.CRITICAL
        )
        assert finding.details["heuristic_hits"][0]["heuristic_id"] == "UNLIMITED"
        assert finding.evidence and "unlimited liability" in finding.evidence[0].text

    async def test_the_models_unsupported_explanation_is_not_shown_as_fact(self) -> None:
        from tests.unit.graphs.analysis_harness import default_assessment

        def invented(prompt: str, rule_id: str):  # type: ignore[no-untyped-def]
            made_up = default_assessment(prompt, rule_id).model_copy(
                update={"explanation": "Termination needs ninety days of written notice by post."}
            )
            return made_up

        a = Analysis(ready_document(), assess=invented)
        await a.run()
        finding = a.by_rule("LIAB-001")
        assert finding.clause_status is ClauseStatus.INSUFFICIENT_EVIDENCE
        assert "ninety days" not in finding.explanation

    async def test_the_models_ambiguous_verdict_is_kept_with_the_rules_action(self) -> None:
        from tests.unit.graphs.analysis_harness import default_assessment

        def vague(prompt: str, rule_id: str):  # type: ignore[no-untyped-def]
            return default_assessment(prompt, rule_id).model_copy(update={"status": "ambiguous"})

        a = Analysis(ready_document(), assess=vague)
        await a.run()
        finding = a.by_rule("LIAB-001")
        assert (
            finding.clause_status is ClauseStatus.AMBIGUOUS and finding.severity is Severity.MEDIUM
        )
        assert (
            finding.recommended_action == RULESET.rule("LIAB-001").recommended_actions["ambiguous"]
        )


class TestFailuresInsideTheRun:
    async def test_a_failed_batch_costs_those_rules_their_assessment_not_the_job(self) -> None:
        def sometimes_down(ids: list[str]) -> None:
            if "SLA-001" in ids:
                raise LLMUnavailable("the model is down")

        a = Analysis(ready_document(), on_batch=sometimes_down)
        await a.run()
        broken = next(b for b in applicable("saas") if b == "SLA-001")
        finding = a.by_rule(broken)
        assert finding.clause_status is ClauseStatus.INSUFFICIENT_EVIDENCE
        assert finding.details["error"] == "LLM_UNAVAILABLE"
        assert a.by_rule("LIAB-001").clause_status is ClauseStatus.ACCEPTABLE

    async def test_a_retrieval_failure_degrades_only_that_rule(self) -> None:
        a = Analysis(ready_document())
        a.retriever.fail_for = ["limitation of liability"]
        await a.run()
        finding = a.by_rule("LIAB-001")
        assert finding.clause_status is ClauseStatus.INSUFFICIENT_EVIDENCE
        assert finding.details["error"] == "RETRIEVAL_FAILED"
        assert a.by_rule("GOV-001").clause_status is ClauseStatus.ACCEPTABLE

    async def test_more_than_half_the_rules_failing_fails_the_job_and_stores_nothing(self) -> None:
        def always_down(ids: list[str]) -> None:
            raise LLMUnavailable("the model is down")

        a = Analysis(ready_document(), on_batch=always_down)
        with pytest.raises(PermanentTaskError) as caught:
            await a.run()
        assert caught.value.job_code == "ANALYSIS_DEGRADED"
        assert a.store.persist_calls == 0

    @pytest.mark.parametrize(("extra", "fails"), [(0, False), (1, True)])
    async def test_the_degraded_threshold_is_more_than_half_of_the_rules(
        self, extra: int, fails: bool
    ) -> None:
        rules = applicable("saas")
        down = set(rules[: len(rules) // 2 + extra])  # one rule per batch, so each fails alone

        def partly_down(ids: list[str]) -> None:
            if down & set(ids):
                raise LLMUnavailable("down")

        a = Analysis(ready_document(), on_batch=partly_down, rules_per_batch=1)
        if fails:
            with pytest.raises(PermanentTaskError) as caught:
                await a.run()
            assert caught.value.job_code == "ANALYSIS_DEGRADED"
        else:
            await a.run()
            statuses = [a.by_rule(r).clause_status for r in down]
            assert set(statuses) == {ClauseStatus.INSUFFICIENT_EVIDENCE}

    async def test_a_document_that_is_not_ready_ends_with_a_clear_code(self) -> None:
        a = Analysis(ready_document(status="processing"))
        with pytest.raises(PermanentTaskError) as caught:
            await a.run()
        assert caught.value.job_code == "DOCUMENT_NOT_READY" and a.llm.calls == 0

    async def test_a_document_that_is_gone_ends_with_the_same_code(self) -> None:
        a = Analysis(ready_document())

        async def gone(*_: Any) -> None:
            return None

        a.store.load_document = gone  # type: ignore[method-assign]
        with pytest.raises(PermanentTaskError) as caught:
            await a.run()
        assert caught.value.job_code == "DOCUMENT_NOT_READY"


class TestExtraPasses:
    async def test_obligations_are_listed_with_validated_deadlines(self) -> None:
        a = Analysis(ready_document())
        a.retriever.text_for["shall notify"] = (
            "The Vendor shall give notice no later than 30 days before renewal."
        )
        a.llm.script(
            "signal_extraction",
            lambda r: ObligationExtraction(
                obligations=[
                    ObligationItem(
                        party="vendor",
                        action="give notice before renewal",
                        deadline_text="no later than 30 days before renewal",
                        deadline_days=30,
                        marker=1,
                    ),
                    ObligationItem(
                        party="customer", action="pay a fee", deadline_days=99, marker=1
                    ),
                ]
            ),
        )
        await a.run()
        obligations = [f for f in a.findings if f.details.get("kind") == "obligation"]
        assert len(obligations) == 2
        assert all(
            o.risk_category is RiskCategory.OBLIGATIONS and o.severity is Severity.INFO
            for o in obligations
        )
        by_action = {o.details["action"]: o for o in obligations}
        assert by_action["give notice before renewal"].details["deadline_days"] == 30
        assert by_action["pay a fee"].details["deadline_days"] is None  # 99 is not in the source
        assert {o.rule_id for o in obligations} == {"OBL-001", "OBL-002"}

    async def test_a_failed_obligation_pass_is_a_warning_not_a_failure(self) -> None:
        a = Analysis(ready_document())
        a.llm.script("signal_extraction", lambda r: (_ for _ in ()).throw(LLMUnavailable("down")))
        await a.run()
        assert "obligations could not be extracted" in a.store.stats[a.job_id]["notes"]

    async def test_a_confirmed_contradiction_becomes_a_finding_with_both_passages(self) -> None:
        a = Analysis(ready_document())
        a.retriever.text_for["payment terms invoices"] = (
            "Invoices are payable within 30 days of receipt."
        )
        a.retriever.text_for["fees payable net"] = (
            "Fees are due within 45 days of the invoice date."
        )
        await a.run()
        found = [f for f in a.findings if f.details.get("kind") == "contradiction"]
        assert len(found) == 1
        finding = found[0]
        assert (
            finding.rule_id == "CONTRA-PAYMENT-DAYS"
            and finding.clause_status is ClauseStatus.PROBLEMATIC
        )
        assert sorted(finding.details["values"]) == ["30", "45"] and len(finding.evidence) == 2

    async def test_a_candidate_the_model_dismisses_is_not_reported(self) -> None:
        from covenantiq.ai.schemas import ContradictionVerdict

        a = Analysis(ready_document())
        a.retriever.text_for["payment terms invoices"] = "Invoices are payable within 30 days."
        a.retriever.text_for["fees payable net"] = "Fees are due within 45 days."
        original = a.llm._handlers["rule_assessment"]

        def handler(request: Any) -> Any:
            if "<value_a>" in request.messages[0].content:
                return ContradictionVerdict(is_contradiction=False, explanation="Different things.")
            return original(request)

        a.llm.script("rule_assessment", handler)
        await a.run()
        assert not [f for f in a.findings if f.details.get("kind") == "contradiction"]

    async def test_unusual_provisions_are_found_anywhere_in_the_document(self) -> None:
        a = Analysis(ready_document())
        a.store.chunks = [
            ChunkText(
                chunk_id=__import__("uuid").uuid4(),
                clause_id=None,
                document_id=a.doc.id,
                document_title="Acme MSA",
                text="1. Definitions. Words have their usual meaning.",
                clause_ref="1",
            ),
            ChunkText(
                chunk_id=__import__("uuid").uuid4(),
                clause_id=None,
                document_id=a.doc.id,
                document_title="Acme MSA",
                text="The Vendor may amend these terms at any time upon notice.",
                clause_ref="14.2",
                page_start=9,
            ),
        ]
        await a.run()
        unusual = [f for f in a.findings if f.details.get("kind") == "unusual"]
        assert [(u.rule_id, u.severity, u.clause_ref, u.page_number) for u in unusual] == [
            ("UNUSUAL-UNILATERAL_AMENDMENT", Severity.HIGH, "14.2", 9)
        ]
        assert "amend these terms" in unusual[0].explanation and len(unusual[0].evidence) == 1

    async def test_the_same_unusual_provision_is_reported_at_most_three_times(self) -> None:
        a = Analysis(ready_document())
        uid = __import__("uuid").uuid4
        a.store.chunks = [
            ChunkText(
                chunk_id=uid(),
                clause_id=None,
                document_id=a.doc.id,
                document_title="t",
                text="Liquidated damages apply.",
                clause_ref=str(i),
            )
            for i in range(6)
        ]
        await a.run()
        hits = [f for f in a.findings if f.rule_id == "UNUSUAL-LIQUIDATED_DAMAGES"]
        assert len(hits) == 3 and [h.ordinal for h in hits] == [0, 1, 2]

    async def test_findings_of_the_same_rule_get_distinct_ordinals(self) -> None:
        a = Analysis(ready_document())
        await a.run()
        keys = [(f.rule_id, f.ordinal) for f in a.findings]
        assert len(keys) == len(set(keys))


class TestValidateEvidence:
    async def test_a_finding_with_no_cited_passage_is_downgraded(self) -> None:
        a = Analysis(ready_document())
        bare = RuleResult(
            rule_id="LIAB-001",
            title="Limitation of liability",
            category=RiskCategory.LIABILITY,
            status=ClauseStatus.PROBLEMATIC,
            severity=Severity.HIGH,
            confidence=0.9,
            evidence_sufficiency=EvidenceSufficiency.STRONG,
            explanation="Looks risky.",
        )
        update = await ContractNodes(a.deps).validate_evidence(
            {"validated_results": [bare.model_dump(mode="json")]}  # type: ignore[typeddict-item]
        )
        downgraded = RuleResult.model_validate(update["validated_results"][0])
        assert downgraded.status is ClauseStatus.INSUFFICIENT_EVIDENCE
        assert downgraded.severity is Severity.INFO and update["warnings"]


class TestResume:
    async def test_a_crash_after_the_batches_resumes_without_asking_the_model_again(self) -> None:
        a = Analysis(ready_document())
        state = {"crashed": False}

        def crash_once(request: Any) -> Any:
            if not state["crashed"]:
                state["crashed"] = True
                raise Crash()
            return ObligationExtraction(obligations=[])

        a.llm.script("signal_extraction", crash_once)
        with pytest.raises(Crash):
            await a.run()
        assessed = list(a.batches_assessed)
        assert assessed and a.store.persist_calls == 0

        outcome = await a.run()  # the redelivered task

        assert outcome.resumed and a.store.persist_calls == 1
        assert a.batches_assessed == assessed  # not one batch was paid for twice
        assert {i for b in a.batches_assessed for i in b} == set(applicable("saas"))

    async def test_a_crash_in_the_middle_of_the_batches_still_completes(self) -> None:
        a = Analysis(ready_document())
        state = {"calls": 0}

        def crash_on_third(ids: list[str]) -> None:
            state["calls"] += 1
            if state["calls"] == 3:
                raise Crash()

        a.on_batch = crash_on_third
        with pytest.raises(Crash):
            await a.run(max_concurrency=1)  # one at a time: two batches finish before the crash
        done_before = list(a.batches_assessed)
        assert len(done_before) == 2

        outcome = await a.run(max_concurrency=1)

        assert outcome.resumed and a.store.persist_calls == 1
        assert not set(done_before) & set(a.batches_assessed[2:])  # the finished two were not rerun
        assert {i for b in a.batches_assessed for i in b} == set(applicable("saas"))
        assert {f.rule_id for f in a.findings} >= set(applicable("saas"))
