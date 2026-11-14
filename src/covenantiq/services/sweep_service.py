from dataclasses import dataclass
from datetime import timedelta

from redis.asyncio import Redis
from redis.exceptions import RedisError

from covenantiq.core.enums import ProcessingStage
from covenantiq.core.logging import get_logger
from covenantiq.services.job_service import ANALYSIS, JobService, QueuedJob
from covenantiq.services.task_dispatcher import TaskDispatcher

log = get_logger(__name__)

STUCK_AFTER = timedelta(minutes=5)
SWEEP_LOCK_SECONDS = 55
REDISPATCH_GUARD_SECONDS = 600


@dataclass(frozen=True)
class SweepResult:
    redispatched: int
    skipped_by_lock: bool = False


class SweepService:
    """Reconciliation: re-publish jobs that are still `queued` long after they were created.

    The commit that creates a job and the publish that announces it are two steps (spec 15.8),
    so a crash or broker outage between them leaves a job nobody will ever run. Re-publishing is
    always safe: a worker that receives a duplicate loses the claim and does nothing.
    """

    def __init__(
        self,
        jobs: JobService,
        dispatcher: TaskDispatcher,
        redis: Redis | None,
        *,
        env: str,
        stuck_after: timedelta = STUCK_AFTER,
    ) -> None:
        self._jobs = jobs
        self._dispatcher = dispatcher
        self._redis = redis
        self._env = env
        self._stuck_after = stuck_after

    async def run_once(self) -> SweepResult:
        if self._redis is not None and not await self._take_lock(self._redis):
            return SweepResult(0, skipped_by_lock=True)

        redispatched = 0
        # jobs whose worker died come back to `queued` first, and are published at once
        for job in await self._jobs.reap_lost():
            try:
                await self._dispatch(job)
            except Exception:
                log.warning("sweep.dispatch_failed", job_id=str(job.job_id))
                continue
            log.warning("sweep.recovered_lost_job", job_id=str(job.job_id))
            redispatched += 1
        for job in await self._jobs.stale_queued(older_than=self._stuck_after):
            if not await self._first_redispatch(job):
                continue
            try:
                await self._dispatch(job)
            except Exception:
                log.warning("sweep.dispatch_failed", job_id=str(job.job_id))
                continue
            redispatched += 1
        if redispatched:
            log.info("sweep.redispatched", count=redispatched)
        return SweepResult(redispatched)

    async def _dispatch(self, job: QueuedJob) -> None:
        if job.kind == ANALYSIS and job.job_type is not None:
            await self._dispatcher.enqueue_analysis(job.job_type, job.job_id)
        elif job.stage == ProcessingStage.EMBEDDING:
            await self._dispatcher.enqueue_embedding(job.job_id)
        else:
            await self._dispatcher.enqueue_ingestion(job.job_id)

    async def _take_lock(self, redis: Redis) -> bool:
        """One sweeper at a time across every API replica and the CLI."""
        try:
            return bool(
                await redis.set(f"ciq:{self._env}:lock:sweep", "1", nx=True, ex=SWEEP_LOCK_SECONDS)
            )
        except (RedisError, OSError):
            log.warning("sweep.lock_unavailable")
            return False

    async def _first_redispatch(self, job: QueuedJob) -> bool:
        """Re-publish each job at most once per guard window, so a long backlog is not flooded
        with duplicates every minute."""
        if self._redis is None:
            return True
        try:
            return bool(
                await self._redis.set(
                    f"ciq:{self._env}:sweep:redispatched:{job.job_id}",
                    "1",
                    nx=True,
                    ex=REDISPATCH_GUARD_SECONDS,
                )
            )
        except (RedisError, OSError):
            return False
