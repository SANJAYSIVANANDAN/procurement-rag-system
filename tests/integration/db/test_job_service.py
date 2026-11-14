import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from covenantiq.core.enums import AnalysisJobType, DocumentStatus, JobStatus
from covenantiq.core.errors import NotFound
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.services.dto import Principal
from covenantiq.services.job_service import (
    ANALYSIS,
    MAX_LOST_ATTEMPTS,
    PROCESSING,
    STALE_HEARTBEAT,
    JobService,
)
from tests.integration.factories import (
    make_analysis_job,
    make_document,
    make_processing_job,
    make_user,
)

UowFactory = Callable[[], UnitOfWork]


@pytest.fixture
def jobs(uow_factory: UowFactory) -> JobService:
    return JobService(uow_factory)


class Seed:
    def __init__(self, owner: UUID, document: UUID, job: UUID) -> None:
        self.owner, self.document, self.job = owner, document, job


async def seed_processing(
    uow_factory: UowFactory,
    *,
    document_status: DocumentStatus = DocumentStatus.QUEUED,
) -> Seed:
    async with uow_factory() as uow:
        user = await uow.users.add(make_user())
        scope = OwnershipScope(user.id)
        document = await uow.documents.add(scope, make_document(user.id, status=document_status))
        job = await uow.processing_jobs.create(scope, make_processing_job(user.id, document.id))
        await uow.commit()
    return Seed(user.id, document.id, job.id)


async def sql(uow_factory: UowFactory, statement: str, **params: object) -> None:
    async with uow_factory() as uow:
        await uow.session.execute(text(statement), params)
        await uow.commit()


async def row(uow_factory: UowFactory, statement: str, **params: object) -> tuple[object, ...]:
    async with uow_factory() as uow:
        result = await uow.session.execute(text(statement), params)
        return tuple(result.one())


class TestClaim:
    async def test_queued_job_is_claimed_and_marked_running(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        claim = await jobs.claim(PROCESSING, seed.job)
        assert (claim.claimed, claim.exists, claim.owner_id, claim.attempt) == (
            True,
            True,
            seed.owner,
            1,
        )
        status, started, heartbeat, attempt = await row(
            uow_factory,
            "SELECT status, started_at IS NOT NULL, heartbeat_at IS NOT NULL, attempt "
            "FROM document_processing_jobs WHERE id = :id",
            id=seed.job,
        )
        assert (status, started, heartbeat, attempt) == ("running", True, True, 1)

    async def test_unknown_job_does_not_exist(self, jobs: JobService) -> None:
        claim = await jobs.claim(PROCESSING, uuid4())
        assert (claim.claimed, claim.exists) == (False, False)

    async def test_eight_concurrent_claims_have_exactly_one_winner(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        claims = await asyncio.gather(*(jobs.claim(PROCESSING, seed.job) for _ in range(8)))
        assert [c.claimed for c in claims].count(True) == 1
        assert all(c.exists for c in claims)
        (attempt,) = await row(
            uow_factory, "SELECT attempt FROM document_processing_jobs WHERE id = :id", id=seed.job
        )
        assert attempt == 1

    async def test_a_running_job_with_a_fresh_heartbeat_cannot_be_taken(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        assert (await jobs.claim(PROCESSING, seed.job)).claimed
        second = await jobs.claim(PROCESSING, seed.job)  # the other worker is alive
        assert (second.claimed, second.exists) == (False, True)

    async def test_a_running_job_with_a_stale_heartbeat_is_taken_over_once(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        assert (await jobs.claim(PROCESSING, seed.job)).claimed
        await sql(
            uow_factory,
            "UPDATE document_processing_jobs SET heartbeat_at = now() - interval '11 minutes' "
            "WHERE id = :id",
            id=seed.job,
        )
        takeovers = await asyncio.gather(*(jobs.claim(PROCESSING, seed.job) for _ in range(5)))
        winners = [c for c in takeovers if c.claimed]
        assert len(winners) == 1
        assert winners[0].attempt == 2  # a redelivery counts as another attempt

    async def test_a_ten_minute_old_heartbeat_is_the_boundary(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        await jobs.claim(PROCESSING, seed.job)
        await sql(
            uow_factory,
            "UPDATE document_processing_jobs SET heartbeat_at = now() - interval '9 minutes' "
            "WHERE id = :id",
            id=seed.job,
        )
        assert not (await jobs.claim(PROCESSING, seed.job)).claimed

    @pytest.mark.parametrize("final", ["succeeded", "failed", "cancelled"])
    async def test_finished_jobs_are_never_claimed(
        self, jobs: JobService, uow_factory: UowFactory, final: str
    ) -> None:
        seed = await seed_processing(uow_factory)
        await sql(
            uow_factory,
            f"UPDATE document_processing_jobs SET status = '{final}' WHERE id = :id",  # noqa: S608
            id=seed.job,
        )
        claim = await jobs.claim(PROCESSING, seed.job)
        assert (claim.claimed, claim.exists) == (False, True)


class TestProgress:
    async def test_touch_records_heartbeat_stage_and_progress(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        await jobs.claim(PROCESSING, seed.job)
        assert await jobs.touch(PROCESSING, seed.job, stage="chunking", progress=0.4)
        stage, progress = await row(
            uow_factory,
            "SELECT stage, progress FROM document_processing_jobs WHERE id = :id",
            id=seed.job,
        )
        assert (stage, progress) == ("chunking", pytest.approx(0.4))

    async def test_a_job_that_is_not_running_is_not_touched(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)  # still queued
        assert not await jobs.touch(PROCESSING, seed.job, progress=0.9)

    async def test_touch_refreshes_the_heartbeat_so_takeover_is_blocked(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        await jobs.claim(PROCESSING, seed.job)
        await sql(
            uow_factory,
            "UPDATE document_processing_jobs SET heartbeat_at = now() - interval '11 minutes' "
            "WHERE id = :id",
            id=seed.job,
        )
        await jobs.touch(PROCESSING, seed.job)  # the slow worker was alive after all
        assert not (await jobs.claim(PROCESSING, seed.job)).claimed


class TestFinish:
    async def test_success_only_follows_running(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        assert not await jobs.mark_succeeded(PROCESSING, seed.job)  # still queued
        await jobs.claim(PROCESSING, seed.job)
        assert await jobs.mark_succeeded(PROCESSING, seed.job)
        status, progress, finished = await row(
            uow_factory,
            "SELECT status, progress, finished_at IS NOT NULL "
            "FROM document_processing_jobs WHERE id = :id",
            id=seed.job,
        )
        assert (status, progress, finished) == ("succeeded", 1.0, True)
        assert not await jobs.mark_succeeded(PROCESSING, seed.job)  # not twice

    async def test_failure_marks_the_job_and_its_document(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        await jobs.claim(PROCESSING, seed.job)
        assert await jobs.mark_failed(
            PROCESSING, seed.job, code="EXTRACTION_FAILED", message="The file could not be read."
        )
        job = await row(
            uow_factory,
            "SELECT status, error_code, error_message, finished_at IS NOT NULL "
            "FROM document_processing_jobs WHERE id = :id",
            id=seed.job,
        )
        document = await row(
            uow_factory, "SELECT status, error_code FROM documents WHERE id = :id", id=seed.document
        )
        assert job == ("failed", "EXTRACTION_FAILED", "The file could not be read.", True)
        assert document == ("failed", "EXTRACTION_FAILED")

    async def test_a_queued_job_can_be_failed_directly(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        assert await jobs.mark_failed(PROCESSING, seed.job, code="X", message="m")

    async def test_a_finished_job_is_not_failed_afterwards(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        await jobs.claim(PROCESSING, seed.job)
        await jobs.mark_succeeded(PROCESSING, seed.job)
        assert not await jobs.mark_failed(PROCESSING, seed.job, code="X", message="m")
        (status,) = await row(
            uow_factory, "SELECT status FROM document_processing_jobs WHERE id = :id", id=seed.job
        )
        assert status == "succeeded"

    async def test_a_ready_document_is_not_downgraded_by_a_late_failure(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory, document_status=DocumentStatus.READY)
        await jobs.mark_failed(PROCESSING, seed.job, code="X", message="m")
        (status,) = await row(
            uow_factory, "SELECT status FROM documents WHERE id = :id", id=seed.document
        )
        assert status == "ready"

    async def test_failing_an_unknown_job_is_a_no_op(self, jobs: JobService) -> None:
        assert not await jobs.mark_failed(PROCESSING, uuid4(), code="X", message="m")


class TestReplayReset:
    async def test_a_failed_job_goes_back_to_queued_with_a_clean_slate(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        await jobs.claim(PROCESSING, seed.job)
        await jobs.touch(PROCESSING, seed.job, stage="chunking", progress=0.6)
        await jobs.mark_failed(PROCESSING, seed.job, code="BOOM", message="exploded")

        assert await jobs.reset_for_replay(PROCESSING, seed.job)

        job = await row(
            uow_factory,
            "SELECT status, error_code, error_message, finished_at, started_at, heartbeat_at, "
            "stage, progress FROM document_processing_jobs WHERE id = :id",
            id=seed.job,
        )
        document = await row(
            uow_factory, "SELECT status, error_code FROM documents WHERE id = :id", id=seed.document
        )
        assert job == ("queued", None, None, None, None, None, None, 0.0)
        assert document == ("queued", None)

    async def test_only_a_failed_job_can_be_reset_and_only_once(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        assert not await jobs.reset_for_replay(PROCESSING, seed.job)  # queued, not failed
        await jobs.mark_failed(PROCESSING, seed.job, code="X", message="m")
        results = await asyncio.gather(
            *(jobs.reset_for_replay(PROCESSING, seed.job) for _ in range(5))
        )
        assert results.count(True) == 1

    async def test_an_unknown_job_cannot_be_reset(self, jobs: JobService) -> None:
        assert not await jobs.reset_for_replay(PROCESSING, uuid4())


class TestStuckJobs:
    async def test_only_old_queued_jobs_are_listed(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        from datetime import timedelta

        old = await seed_processing(uow_factory)
        fresh = await seed_processing(uow_factory)
        running = await seed_processing(uow_factory)
        await jobs.claim(PROCESSING, running.job)
        for seed in (old, running):
            await sql(
                uow_factory,
                "UPDATE document_processing_jobs SET created_at = now() - interval '10 minutes' "
                "WHERE id = :id",
                id=seed.job,
            )

        stuck = await jobs.stale_queued(older_than=timedelta(minutes=5))
        assert [(j.kind, j.job_id) for j in stuck] == [(PROCESSING, old.job)]
        assert fresh.job not in [j.job_id for j in stuck]

    async def test_analysis_jobs_are_listed_with_their_type(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        from datetime import timedelta

        seed = await seed_processing(uow_factory)
        scope = OwnershipScope(seed.owner)
        async with uow_factory() as uow:
            job, _ = await uow.analysis_jobs.create_or_get_active(
                scope, make_analysis_job(seed.owner, document_id=seed.document)
            )
            await uow.commit()
        await sql(
            uow_factory,
            "UPDATE analysis_jobs SET created_at = now() - interval '10 minutes' WHERE id = :id",
            id=job.id,
        )
        stuck = await jobs.stale_queued(older_than=timedelta(minutes=5))
        assert [(j.kind, j.job_id, j.job_type) for j in stuck] == [
            (ANALYSIS, job.id, AnalysisJobType.CONTRACT_ANALYSIS)
        ]


class TestAnalysisKind:
    async def seed(self, uow_factory: UowFactory) -> tuple[Seed, UUID]:
        seed = await seed_processing(uow_factory)
        async with uow_factory() as uow:
            job, _ = await uow.analysis_jobs.create_or_get_active(
                OwnershipScope(seed.owner), make_analysis_job(seed.owner, document_id=seed.document)
            )
            await uow.commit()
        return seed, job.id

    async def test_claim_progress_success_for_analysis_jobs(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        _, job = await self.seed(uow_factory)
        claim = await jobs.claim(ANALYSIS, job)
        assert claim.claimed
        assert not (await jobs.claim(ANALYSIS, job)).claimed
        assert await jobs.touch(ANALYSIS, job, stage="rules", progress=0.5)
        assert await jobs.mark_succeeded(ANALYSIS, job)

    async def test_failure_and_replay_for_analysis_jobs(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        _, job = await self.seed(uow_factory)
        await jobs.claim(ANALYSIS, job)
        assert await jobs.mark_failed(ANALYSIS, job, code="NOT_IMPLEMENTED", message="m")
        (status, code) = await row(
            uow_factory, "SELECT status, error_code FROM analysis_jobs WHERE id = :id", id=job
        )
        assert (status, code) == ("failed", "NOT_IMPLEMENTED")
        assert await jobs.reset_for_replay(ANALYSIS, job)
        (status,) = await row(
            uow_factory, "SELECT status FROM analysis_jobs WHERE id = :id", id=job
        )
        assert status == "queued"


class TestLookup:
    async def test_the_owner_can_read_processing_and_analysis_jobs(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        async with uow_factory() as uow:
            analysis, _ = await uow.analysis_jobs.create_or_get_active(
                OwnershipScope(seed.owner), make_analysis_job(seed.owner, document_id=seed.document)
            )
            await uow.commit()
        principal = Principal(user_id=seed.owner, email="o@example.com")

        processing_dto = await jobs.get(principal, seed.job)
        analysis_dto = await jobs.get(principal, analysis.id)

        assert (processing_dto.kind, processing_dto.status) == (
            "document_processing",
            JobStatus.QUEUED,
        )
        assert (analysis_dto.kind, analysis_dto.status) == ("contract_analysis", JobStatus.QUEUED)

    async def test_other_users_and_unknown_ids_are_not_found(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        stranger = Principal(user_id=uuid4(), email="s@example.com")
        for job_id in (seed.job, uuid4()):
            with pytest.raises(NotFound):
                await jobs.get(stranger, job_id)


class TestReapLost:
    """A worker that dies leaves its job `running`. RabbitMQ redelivers the message at once, but
    a recent heartbeat looks alive, so the redelivery is skipped. The sweep reaps such jobs."""

    async def running(self, uow_factory: UowFactory, *, minutes_ago: int, attempt: int = 1) -> Seed:
        seed = await seed_processing(uow_factory, document_status=DocumentStatus.EXTRACTING)
        await sql(
            uow_factory,
            "UPDATE document_processing_jobs SET status = 'running', stage = 'extracting', "
            "attempt = :attempt, heartbeat_at = now() - make_interval(mins => :minutes) "
            "WHERE id = :id",
            id=seed.job,
            attempt=attempt,
            minutes=minutes_ago,
        )
        return seed

    async def test_a_job_with_a_stale_heartbeat_goes_back_to_queued(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await self.running(uow_factory, minutes_ago=11)

        reaped = await jobs.reap_lost()

        assert [(j.kind, j.job_id) for j in reaped] == [(PROCESSING, seed.job)]
        assert (
            await row(
                uow_factory, "SELECT status FROM document_processing_jobs WHERE id = :i", i=seed.job
            )
        ) == ("queued",)
        # a queued job with a fresh heartbeat is not immediately swept up a second time
        assert await jobs.stale_queued(older_than=timedelta(minutes=5)) == []
        # and it can be claimed again by the republished message
        assert (await jobs.claim(PROCESSING, seed.job)).claimed

    async def test_a_job_whose_worker_is_alive_is_left_alone(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await self.running(uow_factory, minutes_ago=2)
        assert await jobs.reap_lost() == []
        assert (
            await row(
                uow_factory, "SELECT status FROM document_processing_jobs WHERE id = :i", i=seed.job
            )
        ) == ("running",)

    async def test_a_job_that_keeps_losing_its_worker_is_failed_not_retried_forever(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await self.running(uow_factory, minutes_ago=30, attempt=MAX_LOST_ATTEMPTS)

        assert await jobs.reap_lost() == []

        assert (
            await row(
                uow_factory,
                "SELECT status, error_code FROM document_processing_jobs WHERE id = :i",
                i=seed.job,
            )
        ) == ("failed", "WORKER_LOST")
        assert (
            await row(uow_factory, "SELECT status FROM documents WHERE id = :d", d=seed.document)
        ) == ("failed",)

    async def test_the_heartbeat_check_is_part_of_the_update(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        """A worker that heartbeats between the listing and the requeue keeps its job."""
        seed = await self.running(uow_factory, minutes_ago=11)
        await jobs.touch(PROCESSING, seed.job)  # the worker was only slow
        async with uow_factory() as uow:
            cutoff = datetime.now(UTC) - STALE_HEARTBEAT
            assert not await uow.processing_jobs.system_requeue_if_stale(seed.job, cutoff)
        assert (
            await row(
                uow_factory, "SELECT status FROM document_processing_jobs WHERE id = :i", i=seed.job
            )
        ) == ("running",)

    async def test_analysis_jobs_are_reaped_too(
        self, jobs: JobService, uow_factory: UowFactory
    ) -> None:
        seed = await seed_processing(uow_factory)
        async with uow_factory() as uow:
            job, _ = await uow.analysis_jobs.create_or_get_active(
                OwnershipScope(seed.owner), make_analysis_job(seed.owner, document_id=seed.document)
            )
            await uow.commit()
        await sql(
            uow_factory,
            "UPDATE analysis_jobs SET status = 'running', attempt = 1, "
            "heartbeat_at = now() - interval '15 minutes' WHERE id = :id",
            id=job.id,
        )

        reaped = await jobs.reap_lost()

        assert [(j.kind, j.job_id, j.job_type) for j in reaped] == [
            (ANALYSIS, job.id, AnalysisJobType.CONTRACT_ANALYSIS)
        ]
