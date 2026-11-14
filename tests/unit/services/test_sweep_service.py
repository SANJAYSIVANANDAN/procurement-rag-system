from datetime import timedelta
from uuid import UUID, uuid4

from covenantiq.core.enums import AnalysisJobType, ProcessingStage
from covenantiq.services.job_service import ANALYSIS, PROCESSING, QueuedJob
from covenantiq.services.sweep_service import (
    REDISPATCH_GUARD_SECONDS,
    STUCK_AFTER,
    SWEEP_LOCK_SECONDS,
    SweepService,
)
from tests.fakes import FakeRedis, RecordingDispatcher


class FakeJobs:
    def __init__(self, stuck: list[QueuedJob]) -> None:
        self.stuck = stuck
        self.lost: list[QueuedJob] = []
        self.asked_older_than: list[timedelta] = []

    async def reap_lost(self, *, limit: int = 100) -> list[QueuedJob]:
        reaped, self.lost = list(self.lost), []
        return reaped

    async def stale_queued(self, *, older_than: timedelta, limit: int = 100) -> list[QueuedJob]:
        self.asked_older_than.append(older_than)
        return list(self.stuck)


def make(
    stuck: list[QueuedJob],
    redis: FakeRedis | None = None,
    dispatcher: RecordingDispatcher | None = None,
) -> tuple[SweepService, RecordingDispatcher, FakeRedis | None]:
    dispatcher = dispatcher or RecordingDispatcher()
    service = SweepService(
        FakeJobs(stuck),  # type: ignore[arg-type]
        dispatcher,
        redis.as_redis() if redis is not None else None,
        env="test",
    )
    return service, dispatcher, redis


async def test_stuck_processing_jobs_are_republished() -> None:
    first, second = uuid4(), uuid4()
    service, dispatcher, _ = make([QueuedJob(PROCESSING, first), QueuedJob(PROCESSING, second)])
    result = await service.run_once()
    assert result.redispatched == 2
    assert dispatcher.ingestion == [first, second]


async def test_a_job_waiting_for_the_embedding_stage_is_published_as_an_embedding_task() -> None:
    waiting, fresh = uuid4(), uuid4()
    service, dispatcher, _ = make(
        [
            QueuedJob(PROCESSING, waiting, stage=ProcessingStage.EMBEDDING),
            QueuedJob(PROCESSING, fresh, stage=ProcessingStage.EXTRACTING),
        ]
    )
    await service.run_once()
    assert dispatcher.embedding == [waiting]
    assert dispatcher.ingestion == [fresh]


async def test_stuck_analysis_jobs_are_published_by_their_type() -> None:
    job = uuid4()
    service, dispatcher, _ = make([QueuedJob(ANALYSIS, job, AnalysisJobType.VENDOR_ASSESSMENT)])
    await service.run_once()
    assert dispatcher.analysis == [(AnalysisJobType.VENDOR_ASSESSMENT, job)]
    assert dispatcher.ingestion == []


async def test_only_jobs_older_than_five_minutes_are_considered() -> None:
    service, _, _ = make([])
    await service.run_once()
    assert service._jobs.asked_older_than == [timedelta(minutes=5)]  # type: ignore[attr-defined]
    assert STUCK_AFTER == timedelta(minutes=5)


async def test_nothing_stuck_means_nothing_published() -> None:
    service, dispatcher, _ = make([])
    assert (await service.run_once()).redispatched == 0
    assert dispatcher.ingestion == []


async def test_a_failed_publish_does_not_stop_the_rest_of_the_sweep() -> None:
    class FlakyDispatcher(RecordingDispatcher):
        async def enqueue_ingestion(self, job_id: UUID) -> None:
            if len(self.ingestion) == 0 and not getattr(self, "tried", False):
                self.tried = True  # type: ignore[attr-defined]
                raise RuntimeError("broker blip")
            await super().enqueue_ingestion(job_id)

    a, b = uuid4(), uuid4()
    service, dispatcher, _ = make(
        [QueuedJob(PROCESSING, a), QueuedJob(PROCESSING, b)], dispatcher=FlakyDispatcher()
    )
    result = await service.run_once()
    assert result.redispatched == 1
    assert dispatcher.ingestion == [b]


async def test_only_one_sweeper_runs_at_a_time() -> None:
    redis = FakeRedis()
    job = uuid4()
    first, _, _ = make([QueuedJob(PROCESSING, job)], redis)
    second, other_dispatcher, _ = make([QueuedJob(PROCESSING, job)], redis)

    assert (await first.run_once()).redispatched == 1
    skipped = await second.run_once()

    assert skipped.skipped_by_lock
    assert skipped.redispatched == 0
    assert other_dispatcher.ingestion == []
    assert redis.expiries["ciq:test:lock:sweep"] == SWEEP_LOCK_SECONDS


async def test_each_job_is_republished_at_most_once_per_guard_window() -> None:
    redis = FakeRedis()
    job = uuid4()
    service, dispatcher, _ = make([QueuedJob(PROCESSING, job)], redis)

    assert (await service.run_once()).redispatched == 1
    redis.values.pop(
        "ciq:test:lock:sweep"
    )  # a minute later the lock has expired, the guard has not
    assert (await service.run_once()).redispatched == 0

    assert dispatcher.ingestion == [job]
    assert redis.expiries[f"ciq:test:sweep:redispatched:{job}"] == REDISPATCH_GUARD_SECONDS


async def test_when_redis_is_down_the_sweep_does_nothing_rather_than_flood() -> None:
    service, dispatcher, _ = make([QueuedJob(PROCESSING, uuid4())], FakeRedis(down=True))
    result = await service.run_once()
    assert result.skipped_by_lock
    assert dispatcher.ingestion == []


async def test_without_redis_the_cli_style_sweep_still_works() -> None:
    job = uuid4()
    service, dispatcher, _ = make([QueuedJob(PROCESSING, job)], redis=None)
    assert (await service.run_once()).redispatched == 1
    assert dispatcher.ingestion == [job]


async def test_jobs_lost_with_their_worker_are_published_again_at_once() -> None:
    lost_processing, lost_embedding, lost_analysis = uuid4(), uuid4(), uuid4()
    service, dispatcher, _ = make([])
    service._jobs.lost = [  # type: ignore[attr-defined]
        QueuedJob(PROCESSING, lost_processing, stage=ProcessingStage.EXTRACTING),
        QueuedJob(PROCESSING, lost_embedding, stage=ProcessingStage.EMBEDDING),
        QueuedJob(ANALYSIS, lost_analysis, AnalysisJobType.CONTRACT_ANALYSIS),
    ]

    result = await service.run_once()

    assert result.redispatched == 3
    assert dispatcher.ingestion == [lost_processing]
    assert dispatcher.embedding == [lost_embedding]
    assert dispatcher.analysis == [(AnalysisJobType.CONTRACT_ANALYSIS, lost_analysis)]


async def test_a_failed_publish_of_a_lost_job_does_not_stop_the_sweep() -> None:
    first, second = uuid4(), uuid4()
    service, dispatcher, _ = make(
        [QueuedJob(PROCESSING, second)], dispatcher=RecordingDispatcher(fail=True)
    )
    service._jobs.lost = [QueuedJob(PROCESSING, first)]  # type: ignore[attr-defined]
    result = await service.run_once()
    assert result.redispatched == 0
    assert dispatcher.ingestion == []
