"""Contract analysis on real PostgreSQL: starting, running, persisting, reading, resuming."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from covenantiq.ai.llm.base import LLMUnavailable
from covenantiq.ai.schemas import ObligationExtraction
from covenantiq.analysis.contract.types import FindingDraft, SourceRef
from covenantiq.core.enums import (
    ClauseStatus,
    ContractType,
    DocumentStatus,
    EvidenceSufficiency,
    JobStatus,
    RiskCategory,
    Severity,
)
from covenantiq.core.errors import (
    DispatchFailed,
    DocumentNotReady,
    NotFound,
    PermanentTaskError,
    ValidationFailed,
)
from covenantiq.db.models import Citation, ContractFinding
from covenantiq.services.dto import Principal
from covenantiq.services.job_service import ANALYSIS
from tests.integration.analysis.stack import (
    RULESET,
    Stack,
    UowFactory,
    build_stack,
    clean_checkpoints,  # noqa: F401  (a fixture)
)
from tests.integration.chat.stack import seed_document
from tests.integration.memory.helpers import new_user
from tests.unit.graphs.analysis_harness import Crash

pytestmark = pytest.mark.usefixtures("clean_checkpoints")


@pytest.fixture
async def stack(uow_factory: UowFactory, database_url: str) -> Stack:
    return await build_stack(uow_factory, database_url)


async def job_row(stack: Stack, job_id):  # type: ignore[no-untyped-def]
    async with stack.uow_factory() as uow:
        job = await uow.analysis_jobs.system_get(job_id)
        assert job is not None
        return job


async def counts(stack: Stack) -> tuple[int, int]:
    async with stack.uow_factory() as uow:
        findings = (
            await uow.session.execute(select(func.count()).select_from(ContractFinding))
        ).scalar_one()
        citations = (
            await uow.session.execute(
                select(func.count()).select_from(Citation).where(Citation.finding_id.is_not(None))
            )
        ).scalar_one()
    return int(findings), int(citations)


async def analyse(stack: Stack, **kw: Any):  # type: ignore[no-untyped-def]
    started = await stack.service.start(stack.principal, stack.document_id, **kw)
    result = await stack.service.run(started.job.id)
    return started, result


class TestStarting:
    async def test_it_creates_a_queued_job_stamped_with_the_ruleset_and_queues_the_task(
        self, stack: Stack
    ) -> None:
        started = await stack.service.start(stack.principal, stack.document_id)
        assert started.created and started.job.status is JobStatus.QUEUED
        assert started.ruleset_version == "contract_baseline_v1@1.0.0"
        assert [j for _, j in stack.dispatcher.analysis] == [started.job.id]
        assert (await job_row(stack, started.job.id)).params["contract_type"] == "unknown"

    async def test_asking_again_while_it_runs_returns_the_same_job_and_queues_nothing_more(
        self, stack: Stack
    ) -> None:
        first = await stack.service.start(stack.principal, stack.document_id)
        again = await stack.service.start(stack.principal, stack.document_id, force=True)
        assert not again.created and again.job.id == first.job.id
        assert len(stack.dispatcher.analysis) == 1

    async def test_a_current_analysis_is_returned_unless_forced(self, stack: Stack) -> None:
        first, _ = await analyse(stack)
        current = await stack.service.start(stack.principal, stack.document_id)
        assert not current.created and current.job.id == first.job.id
        forced = await stack.service.start(stack.principal, stack.document_id, force=True)
        assert forced.created and forced.job.id != first.job.id

    async def test_a_document_changed_after_the_analysis_is_analysed_again(
        self, stack: Stack
    ) -> None:
        first, _ = await analyse(stack)
        async with stack.uow_factory() as uow:
            await uow.session.execute(
                text("UPDATE documents SET updated_at = :t WHERE id = :id"),
                {"t": datetime.now(UTC) + timedelta(minutes=1), "id": stack.document_id},
            )
            await uow.commit()
        again = await stack.service.start(stack.principal, stack.document_id)
        assert again.created and again.job.id != first.job.id

    async def test_a_document_that_is_not_ready_cannot_be_analysed(self, stack: Stack) -> None:
        async with stack.uow_factory() as uow:
            await uow.documents.system_set_status(
                stack.document_id, [DocumentStatus.READY], DocumentStatus.EMBEDDING
            )
            await uow.commit()
        with pytest.raises(DocumentNotReady):
            await stack.service.start(stack.principal, stack.document_id)
        assert stack.dispatcher.analysis == []

    async def test_someone_elses_document_is_not_found(
        self, stack: Stack, uow_factory: UowFactory
    ) -> None:
        other = Principal(user_id=(await new_user(uow_factory)).owner_id, email="o@example.com")
        with pytest.raises(NotFound):
            await stack.service.start(other, stack.document_id)

    async def test_an_unknown_ruleset_is_refused(self, stack: Stack) -> None:
        with pytest.raises(ValidationFailed):
            await stack.service.start(stack.principal, stack.document_id, ruleset="made_up")

    async def test_a_failed_publish_leaves_a_queued_job_for_the_sweep(self, stack: Stack) -> None:
        stack.dispatcher.fail = True
        with pytest.raises(DispatchFailed):
            await stack.service.start(stack.principal, stack.document_id)
        async with stack.uow_factory() as uow:
            jobs = await uow.analysis_jobs.system_list_queued_before(
                datetime.now(UTC) + timedelta(hours=1), 10
            )
        assert len(jobs) == 1


class TestRunning:
    async def test_the_job_succeeds_with_findings_citations_and_stats(self, stack: Stack) -> None:
        started, result = await analyse(stack)
        assert result["status"] == "succeeded"

        job = await job_row(stack, started.job.id)
        assert job.status is JobStatus.SUCCEEDED and job.progress == 1.0 and job.finished_at
        expected = len(RULESET.applicable(ContractType.UNKNOWN))
        assert job.params["stats"]["by_status"]["acceptable"] >= expected
        findings, citations = await counts(stack)
        assert findings >= expected
        assert citations >= expected

        async with stack.uow_factory() as uow:
            rows = list(
                (
                    await uow.session.execute(
                        select(Citation).where(Citation.finding_id.is_not(None))
                    )
                ).scalars()
            )
        assert all(c.validated and c.quote and c.document_title == "Acme MSA" for c in rows)
        assert all(c.chunk_id is not None and c.marker == 1 for c in rows)

    async def test_a_second_delivery_of_a_finished_job_does_nothing(self, stack: Stack) -> None:
        started, _ = await analyse(stack)
        calls = stack.llm.calls
        before = await counts(stack)
        again = await stack.service.run(started.job.id)
        assert again == {"status": "skipped"} and stack.llm.calls == calls
        assert await counts(stack) == before

    async def test_persisting_twice_leaves_one_set_of_findings(self, stack: Stack) -> None:
        started, _ = await analyse(stack)
        before = await counts(stack)
        source = SourceRef(
            marker=1,
            chunk_ids=[],
            document_id=stack.document_id,
            document_title="Acme MSA",
            text="The fee cap is twelve months of fees.",
        )
        draft = FindingDraft(
            rule_id="LIAB-001",
            risk_category=RiskCategory.LIABILITY,
            clause_status=ClauseStatus.ACCEPTABLE,
            severity=Severity.INFO,
            title="t",
            explanation="The fee cap is twelve months of fees.",
            confidence=0.7,
            evidence_sufficiency=EvidenceSufficiency.MODERATE,
            evidence=[source],
        )
        for _ in range(2):  # the same call twice: a replay after a crash during persist
            await stack.store.persist(
                stack.scope.owner_id, started.job.id, stack.document_id, [draft], {"by_status": {}}
            )
        assert await counts(stack) == (1, 1)
        assert before[0] > 1
        assert (await job_row(stack, started.job.id)).status is JobStatus.SUCCEEDED

    async def test_a_failed_analysis_leaves_no_partial_findings(self, stack: Stack) -> None:
        def down(request: Any) -> Any:
            raise LLMUnavailable("down")

        stack.llm.script("rule_assessment", down)
        started = await stack.service.start(stack.principal, stack.document_id)
        with pytest.raises(PermanentTaskError, match="could not be assessed"):
            await stack.service.run(started.job.id)
        assert await counts(stack) == (0, 0)
        # the task base class is what marks the job failed (tested with the workers)
        assert (await job_row(stack, started.job.id)).status is JobStatus.RUNNING

    async def test_progress_reaches_the_job_row_while_it_runs(self, stack: Stack) -> None:
        seen: list[tuple[str | None, float]] = []
        original = stack.jobs.touch

        async def spy(kind, job_id, **fields):  # type: ignore[no-untyped-def]
            if "progress" in fields:
                seen.append((fields.get("stage"), fields["progress"]))
            return await original(kind, job_id, **fields)

        stack.jobs.touch = spy  # type: ignore[method-assign]
        await analyse(stack)
        stages = [s for s, _ in seen]
        assert "assessing_rules" in stages and "obligations" in stages
        assert [p for _, p in seen] == sorted(p for _, p in seen)


class TestResume:
    async def test_a_worker_dying_after_the_batches_resumes_from_the_real_checkpoints(
        self, stack: Stack
    ) -> None:
        state = {"crashed": False}

        def crash_once(request: Any) -> Any:
            if not state["crashed"]:
                state["crashed"] = True
                raise Crash()
            return ObligationExtraction(obligations=[])

        stack.llm.script("signal_extraction", crash_once)
        started = await stack.service.start(stack.principal, stack.document_id)
        with pytest.raises(Crash):
            await stack.service.run(started.job.id)
        assessed = list(stack.batch_calls)
        assert assessed and await counts(stack) == (0, 0)

        await stack.jobs.release(ANALYSIS, started.job.id)  # what a retry does before redelivery
        result = await stack.service.run(started.job.id)

        assert result["status"] == "succeeded" and result["resumed"] is True
        assert stack.batch_calls == assessed  # not one batch asked for again
        assert (await counts(stack))[0] > 0
        async with stack.uow_factory() as uow:
            left = (
                await uow.session.execute(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"),
                    {"t": str(started.job.id)},
                )
            ).scalar_one()
        assert left == 0  # the finished job's checkpoints are cleaned up


class TestReadingFindings:
    async def test_the_latest_analysis_is_returned_with_a_summary_sorted_by_severity(
        self, stack: Stack
    ) -> None:
        stack.retriever.set_passage("The Supplier accepts unlimited liability for any loss.")
        started, _ = await analyse(stack)

        report = await stack.service.findings(stack.principal, stack.document_id)

        assert report.job_id == started.job.id
        assert report.ruleset_version == "contract_baseline_v1@1.0.0"
        assert report.disclaimer.startswith(
            "Automated analysis for decision support. Not legal advice."
        )
        assert report.findings[0].severity is Severity.CRITICAL  # most severe first
        ranks = [list(Severity).index(f.severity) for f in report.findings]
        assert ranks == sorted(ranks, reverse=True)
        assert report.summary["critical"] >= 1
        assert set(report.summary) >= {"absent", "insufficient_evidence", "info"}
        assert report.findings[0].evidence and report.findings[0].evidence[0].validated

    async def test_filters_narrow_the_list_but_not_the_summary(self, stack: Stack) -> None:
        stack.retriever.set_passage("The Supplier accepts unlimited liability for any loss.")
        await analyse(stack)
        p, d = stack.principal, stack.document_id
        full = await stack.service.findings(p, d)
        critical = await stack.service.findings(p, d, severity=Severity.CRITICAL)
        liability = await stack.service.findings(p, d, category=RiskCategory.LIABILITY)
        problematic = await stack.service.findings(p, d, status=ClauseStatus.PROBLEMATIC)
        assert 0 < len(critical.findings) < len(full.findings)
        assert all(f.risk_category is RiskCategory.LIABILITY for f in liability.findings)
        assert all(f.clause_status is ClauseStatus.PROBLEMATIC for f in problematic.findings)
        assert critical.summary == full.summary

    async def test_a_specific_job_can_be_asked_for(self, stack: Stack) -> None:
        first, _ = await analyse(stack)
        second, _ = await analyse(stack, force=True)
        latest = await stack.service.findings(stack.principal, stack.document_id)
        assert latest.job_id == second.job.id
        old = await stack.service.findings(stack.principal, stack.document_id, job_id=first.job.id)
        assert old.job_id == first.job.id

    async def test_nothing_is_returned_before_an_analysis_has_succeeded(self, stack: Stack) -> None:
        with pytest.raises(NotFound):
            await stack.service.findings(stack.principal, stack.document_id)
        await stack.service.start(stack.principal, stack.document_id)  # queued, not succeeded
        with pytest.raises(NotFound):
            await stack.service.findings(stack.principal, stack.document_id)

    async def test_other_users_and_other_documents_are_not_found(
        self, stack: Stack, uow_factory: UowFactory
    ) -> None:
        started, _ = await analyse(stack)
        other = Principal(user_id=(await new_user(uow_factory)).owner_id, email="o@example.com")
        with pytest.raises(NotFound):
            await stack.service.findings(other, stack.document_id)
        with pytest.raises(NotFound):
            await stack.service.findings(other, stack.document_id, job_id=started.job.id)
        second_document, _, _ = await seed_document(uow_factory, stack.scope, title="Other")
        with pytest.raises(NotFound):  # a job that belongs to another document of the same user
            await stack.service.findings(stack.principal, second_document, job_id=started.job.id)
        with pytest.raises(NotFound):
            await stack.service.findings(stack.principal, uuid4())
