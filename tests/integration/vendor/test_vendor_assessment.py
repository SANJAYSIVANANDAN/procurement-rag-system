"""Vendor assessment on real PostgreSQL: dependencies, storage, the latest flip, reading."""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from covenantiq.ai.schemas import DimensionExplanation
from covenantiq.core.enums import (
    AnalysisJobType,
    DimensionStatus,
    DocumentStatus,
    JobStatus,
    RiskDimension,
)
from covenantiq.core.errors import NotFound, ValidationFailed
from covenantiq.db.models import Citation, VendorRiskAssessment, VendorRiskDimension
from covenantiq.services.dto import Principal
from covenantiq.services.job_service import ANALYSIS
from tests.integration.analysis.stack import clean_checkpoints  # noqa: F401  (a fixture)
from tests.integration.memory.helpers import new_user
from tests.integration.vendor.stack import (
    UowFactory,
    VendorStack,
    build_vendor_stack,
    set_status,
)
from tests.unit.graphs.analysis_harness import Crash

pytestmark = pytest.mark.usefixtures("clean_checkpoints")


@pytest.fixture
async def stack(uow_factory: UowFactory, database_url: str) -> VendorStack:
    return await build_vendor_stack(uow_factory, database_url)


async def job_row(stack: VendorStack, job_id: UUID):  # type: ignore[no-untyped-def]
    async with stack.uow_factory() as uow:
        job = await uow.analysis_jobs.system_get(job_id)
        assert job is not None
        return job


async def count(stack: VendorStack, model: Any, *where: Any) -> int:
    async with stack.uow_factory() as uow:
        statement = select(func.count()).select_from(model)
        for clause in where:
            statement = statement.where(clause)
        return int((await uow.session.execute(statement)).scalar_one())


class TestStarting:
    async def test_it_starts_the_missing_contract_analyses_and_queues_the_assessment_behind_them(
        self, stack: VendorStack
    ) -> None:
        started = await stack.assessments.start(stack.principal, stack.vendor_id)

        assert started.created and started.job.status is JobStatus.QUEUED
        assert started.scoring_config_version == "vendor_scoring_v1@1.0.0"
        assert len(started.pending_dependencies) == 2
        assert all(d.startswith("contract_analysis:") for d in started.pending_dependencies)
        queued = [(t.value, j) for t, j in stack.dispatcher.analysis]
        assert sorted(t for t, _ in queued) == ["contract_analysis"] * 2 + ["vendor_assessment"]
        job = await job_row(stack, started.job.id)
        assert (
            job.job_type is AnalysisJobType.VENDOR_ASSESSMENT and job.vendor_id == stack.vendor_id
        )
        assert len(job.params["depends_on"]) == 2 and len(job.params["document_ids"]) == 2

    async def test_asking_again_while_it_runs_returns_the_same_job_and_queues_nothing(
        self, stack: VendorStack
    ) -> None:
        first = await stack.assessments.start(stack.principal, stack.vendor_id)
        queued = len(stack.dispatcher.analysis)
        again = await stack.assessments.start(stack.principal, stack.vendor_id)
        assert not again.created and again.job.id == first.job.id
        assert again.pending_dependencies == first.pending_dependencies
        assert len(stack.dispatcher.analysis) == queued

    async def test_with_current_analyses_there_is_nothing_to_wait_for(
        self, stack: VendorStack
    ) -> None:
        await stack.assess()
        queued = len(stack.dispatcher.analysis)
        second = await stack.assessments.start(stack.principal, stack.vendor_id)
        assert second.created and second.pending_dependencies == []
        assert len(stack.dispatcher.analysis) == queued + 1  # only the assessment itself

    async def test_a_vendor_without_ready_documents_cannot_be_assessed(
        self, stack: VendorStack
    ) -> None:
        for document_id in stack.document_ids:
            await set_status(stack, document_id, DocumentStatus.FAILED)
        with pytest.raises(ValidationFailed, match="no ready documents"):
            await stack.assessments.start(stack.principal, stack.vendor_id)

    async def test_another_users_or_an_unknown_vendor_is_not_found(
        self, stack: VendorStack, uow_factory: UowFactory
    ) -> None:
        other = Principal(user_id=(await new_user(uow_factory)).owner_id, email="o@example.com")
        with pytest.raises(NotFound):
            await stack.assessments.start(other, stack.vendor_id)
        with pytest.raises(NotFound):
            await stack.assessments.start(stack.principal, uuid4())


class TestRunning:
    async def test_it_waits_while_a_contract_analysis_it_depends_on_is_still_running(
        self, stack: VendorStack
    ) -> None:
        started = await stack.assessments.start(stack.principal, stack.vendor_id)

        result = await stack.assessments.run(started.job.id)

        assert result["status"] == "waiting" and len(result["pending"]) == 2
        assert (await job_row(stack, started.job.id)).status is JobStatus.QUEUED  # released
        assert stack.llm.calls == 0

    async def test_once_they_finish_it_runs_and_stores_the_assessment(
        self, stack: VendorStack
    ) -> None:
        job_id = await stack.assess()

        job = await job_row(stack, job_id)
        assert job.status is JobStatus.SUCCEEDED and job.progress == 1.0
        async with stack.uow_factory() as uow:
            assessment = await uow.assessments.latest(stack.scope, stack.vendor_id)
        assert assessment is not None and assessment.is_latest
        assert assessment.analysis_job_id == job_id
        assert assessment.scoring_config_version == "vendor_scoring_v1@1.0.0"
        assert assessment.overall_score == 10 and not assessment.provisional
        assert len(assessment.documents_considered) == 2
        assert {d.dimension for d in assessment.dimensions} == set(RiskDimension)
        assert all(
            d.status is DimensionStatus.ASSESSED and d.reasoning for d in assessment.dimensions
        )
        assert sum(assessment.weights.values()) == pytest.approx(1.0)

    async def test_each_dimension_has_citations_for_its_evidence(self, stack: VendorStack) -> None:
        await stack.assess()
        async with stack.uow_factory() as uow:
            rows = list(
                (
                    await uow.session.execute(
                        select(Citation).where(Citation.risk_dimension_id.is_not(None))
                    )
                ).scalars()
            )
            dimensions = {
                d.id: d.dimension
                for d in (await uow.session.execute(select(VendorRiskDimension))).scalars()
            }
        assert {dimensions[c.risk_dimension_id] for c in rows} == set(RiskDimension)  # type: ignore[index]
        assert all(c.validated and c.quote and c.document_title for c in rows)
        assert all(c.finding_id is None and c.message_id is None for c in rows)

    async def test_the_new_assessment_becomes_the_only_latest_and_history_keeps_the_old(
        self, stack: VendorStack
    ) -> None:
        first = await stack.assess()
        second = await stack.assess()

        assert await count(stack, VendorRiskAssessment) == 2
        assert (
            await count(stack, VendorRiskAssessment, VendorRiskAssessment.is_latest.is_(True)) == 1
        )
        async with stack.uow_factory() as uow:
            latest = await uow.assessments.latest(stack.scope, stack.vendor_id)
        assert latest is not None and latest.analysis_job_id == second != first
        history = await stack.assessments.history(stack.principal, stack.vendor_id)
        assert history[0].id == latest.id and len(history) == 2

    async def test_running_it_twice_for_one_job_leaves_one_assessment(
        self, stack: VendorStack
    ) -> None:
        job_id = await stack.assess()
        again = await stack.assessments.run(job_id)
        assert again == {"status": "skipped"}  # a succeeded job cannot be claimed again
        assert await count(stack, VendorRiskAssessment) == 1

    async def test_persisting_the_same_job_again_replaces_its_assessment(
        self, stack: VendorStack
    ) -> None:
        from covenantiq.services.vendor_store import PostgresVendorStore

        job_id = await stack.assess()
        latest = await stack.assessments.latest(stack.principal, stack.vendor_id)
        store = PostgresVendorStore(stack.uow_factory, stack.jobs)
        record = await _record_of(stack, latest.id)
        for _ in range(2):
            await store.persist_assessment(stack.scope.owner_id, job_id, record)
        assert await count(stack, VendorRiskAssessment) == 1
        assert await count(stack, VendorRiskDimension) == 6

    async def test_a_failed_contract_analysis_does_not_stop_the_assessment(
        self, stack: VendorStack
    ) -> None:
        started = await stack.assessments.start(stack.principal, stack.vendor_id)
        contract_jobs = [
            j for t, j in stack.dispatcher.analysis if t is AnalysisJobType.CONTRACT_ANALYSIS
        ]
        await stack.contract.run(contract_jobs[0])
        await stack.jobs.mark_failed(ANALYSIS, contract_jobs[1], code="BOOM", message="x")

        result = await stack.assessments.run(started.job.id)

        assert result["status"] == "succeeded"
        dto = await stack.assessments.latest(stack.principal, stack.vendor_id)
        assert any("failed" in note for note in dto.notes)

    async def test_the_assessment_job_waits_for_a_running_analysis_not_for_a_failed_one(
        self, stack: VendorStack
    ) -> None:
        started = await stack.assessments.start(stack.principal, stack.vendor_id)
        contract_jobs = [
            j for t, j in stack.dispatcher.analysis if t is AnalysisJobType.CONTRACT_ANALYSIS
        ]
        await stack.jobs.mark_failed(ANALYSIS, contract_jobs[0], code="BOOM", message="x")
        await stack.contract.run(contract_jobs[1])
        assert (await stack.assessments.run(started.job.id))["status"] == "succeeded"


async def _record_of(stack: VendorStack, assessment_id: UUID):  # type: ignore[no-untyped-def]
    """Rebuild the record a run stored, from the stored rows."""
    from covenantiq.analysis.vendor.types import (
        AssessmentScore,
        DimensionScore,
        MissingInformation,
        SignalScore,
    )
    from covenantiq.graphs.vendor_assessment.deps import AssessmentRecord

    async with stack.uow_factory() as uow:
        assessment = await uow.assessments.latest(stack.scope, stack.vendor_id)
        assert assessment is not None and assessment.id == assessment_id
        dims = [
            DimensionScore(
                dimension=d.dimension,
                status=d.status,
                score=d.score,
                severity=d.severity,
                coverage=d.coverage,
                signals=[SignalScore.model_validate(s) for s in d.signals],
                missing_information=[
                    MissingInformation.model_validate(m) for m in d.missing_information
                ],
            )
            for d in assessment.dimensions
        ]
        return AssessmentRecord(
            vendor_id=assessment.vendor_id,
            scoring_config_version=assessment.scoring_config_version,
            weights=dict(assessment.weights),
            documents=list(assessment.documents_considered),
            score=AssessmentScore(
                overall_score=assessment.overall_score,
                overall_severity=assessment.overall_severity,
                evidence_coverage=assessment.evidence_coverage,
                completeness=assessment.completeness,
                provisional=assessment.provisional,
                dimensions=dims,
                assessed_dimensions=[
                    d.dimension for d in dims if d.status is DimensionStatus.ASSESSED
                ],
            ),
            dimension_signals={d.dimension.value: list(d.signals) for d in assessment.dimensions},
            reasoning={d.dimension.value: d.reasoning or "" for d in assessment.dimensions},
            mitigations={d.dimension.value: [] for d in assessment.dimensions},
            evidence={},
        )


class TestReading:
    async def test_the_latest_assessment_comes_back_whole(self, stack: VendorStack) -> None:
        await stack.assess()
        dto = await stack.assessments.latest(stack.principal, stack.vendor_id)

        assert dto.overall_score == 10 and dto.scoring_config_version == "vendor_scoring_v1@1.0.0"
        assert [d.dimension for d in dto.dimensions] == list(RiskDimension)  # a stable order
        assert (
            sum(dto.effective_weights.values()) == pytest.approx(1.0)
            and len(dto.effective_weights) == 6
        )
        legal = dto.dimensions[0]
        assert legal.reasoning and legal.evidence and legal.evidence[0].validated
        assert legal.signals[0]["signal_id"] and "observations" in legal.signals[0]
        assert {d["title"] for d in dto.documents_considered} == {
            "Acme contract 1",
            "Acme contract 2",
        }

    async def test_effective_weights_are_renormalised_when_a_dimension_is_not_assessed(
        self, stack: VendorStack
    ) -> None:
        await stack.assess()
        async with stack.uow_factory() as uow:
            await uow.session.execute(
                text(
                    "UPDATE vendor_risk_dimensions SET status = 'not_assessed' "
                    "WHERE dimension = 'compliance'"
                )
            )
            await uow.commit()
        dto = await stack.assessments.latest(stack.principal, stack.vendor_id)
        assert "compliance" not in dto.effective_weights and len(dto.effective_weights) == 5
        assert sum(dto.effective_weights.values()) == pytest.approx(1.0)
        assert dto.weights["compliance"] == 0.10  # the configured weights are unchanged

    async def test_no_assessment_yet_is_not_found(self, stack: VendorStack) -> None:
        with pytest.raises(NotFound):
            await stack.assessments.latest(stack.principal, stack.vendor_id)
        assert await stack.assessments.history(stack.principal, stack.vendor_id) == []

    async def test_other_users_cannot_read_it(
        self, stack: VendorStack, uow_factory: UowFactory
    ) -> None:
        await stack.assess()
        other = Principal(user_id=(await new_user(uow_factory)).owner_id, email="o@example.com")
        for call in (stack.assessments.latest, stack.assessments.history):
            with pytest.raises(NotFound):
                await call(other, stack.vendor_id)


class TestResume:
    async def test_a_worker_dying_after_the_extraction_resumes_without_reading_again(
        self, stack: VendorStack
    ) -> None:
        state = {"crashed": False}

        def crash_once(request: Any) -> Any:
            if not state["crashed"]:
                state["crashed"] = True
                raise Crash()
            return DimensionExplanation(reasoning="A summary.", mitigations=[])

        stack.llm.script("explain", crash_once)
        started = await stack.assessments.start(stack.principal, stack.vendor_id)
        await stack.run_pending_contract_analyses()
        with pytest.raises(Crash):
            await stack.assessments.run(started.job.id)
        calls = len(stack.extraction_calls)
        assert calls == 5 and await count(stack, VendorRiskAssessment) == 0

        await stack.jobs.release(ANALYSIS, started.job.id)
        result = await stack.assessments.run(started.job.id)

        assert result["status"] == "succeeded" and result["resumed"] is True
        assert len(stack.extraction_calls) == calls
        async with stack.uow_factory() as uow:
            left = (
                await uow.session.execute(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"),
                    {"t": str(started.job.id)},
                )
            ).scalar_one()
        assert left == 0
