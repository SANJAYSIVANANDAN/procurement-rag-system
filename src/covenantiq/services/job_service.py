from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from covenantiq.core.enums import AnalysisJobType, DocumentStatus, JobStatus, ProcessingStage
from covenantiq.core.errors import NotFound
from covenantiq.db.uow import UnitOfWork
from covenantiq.services.dto import JobDTO, Principal, analysis_job_to_dto, job_to_dto

JobKind = Literal["document_processing", "analysis"]
PROCESSING: JobKind = "document_processing"
ANALYSIS: JobKind = "analysis"

# A running job whose heartbeat is older than this has lost its worker and may be taken over
# by a redelivered message (spec 18.10).
STALE_HEARTBEAT = timedelta(minutes=10)

# A job whose worker has vanished this many times is failed instead of tried again, so a
# task that kills its worker (out of memory, say) cannot loop forever.
MAX_LOST_ATTEMPTS = 5
LOST_WORKER_CODE = "WORKER_LOST"

_OPEN_DOCUMENT_STATUSES = [
    DocumentStatus.UPLOADED,
    DocumentStatus.QUEUED,
    DocumentStatus.EXTRACTING,
    DocumentStatus.CHUNKING,
    DocumentStatus.EMBEDDING,
]


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    exists: bool
    owner_id: UUID | None = None
    attempt: int = 0


@dataclass(frozen=True)
class QueuedJob:
    kind: JobKind
    job_id: UUID
    job_type: AnalysisJobType | None = None
    stage: ProcessingStage | None = None  # processing jobs: the stage waiting to run


class JobService:
    """Job state transitions. Every change is a compare-and-set, so duplicate deliveries,
    concurrent workers and the reconciliation sweep can never double-run or overwrite a job."""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    # ---- user-facing ----------------------------------------------------------------

    async def get(self, principal: Principal, job_id: UUID) -> JobDTO:
        scope = principal.scope()
        async with self._uow_factory() as uow:
            processing = await uow.processing_jobs.get(scope, job_id)
            if processing is not None:
                return job_to_dto(processing)
            analysis = await uow.analysis_jobs.get(scope, job_id)
            if analysis is not None:
                return analysis_job_to_dto(analysis)
        raise NotFound("Job not found.")

    # ---- worker-facing --------------------------------------------------------------

    async def claim(self, kind: JobKind, job_id: UUID) -> ClaimResult:
        """queued -> running for exactly one caller (or a takeover of a stale running job)."""
        stale_before = datetime.now(UTC) - STALE_HEARTBEAT
        async with self._uow_factory() as uow:
            repo: Any = uow.processing_jobs if kind == PROCESSING else uow.analysis_jobs
            claimed = await repo.system_claim(job_id, stale_before)
            if claimed is not None:
                await uow.commit()
                return ClaimResult(
                    claimed=True, exists=True, owner_id=claimed[0], attempt=claimed[1]
                )
            return ClaimResult(claimed=False, exists=await repo.system_get(job_id) is not None)

    async def touch(
        self,
        kind: JobKind,
        job_id: UUID,
        *,
        stage: str | None = None,
        progress: float | None = None,
    ) -> bool:
        """Heartbeat, optionally with the current stage and progress."""
        fields: dict[str, Any] = {}
        if stage is not None:
            fields["stage"] = ProcessingStage(stage) if kind == PROCESSING else stage
        if progress is not None:
            fields["progress"] = progress
        async with self._uow_factory() as uow:
            repo: Any = uow.processing_jobs if kind == PROCESSING else uow.analysis_jobs
            touched: bool = await repo.system_touch(job_id, **fields)
            await uow.commit()
            return touched

    async def stage_of(self, job_id: UUID) -> ProcessingStage | None:
        async with self._uow_factory() as uow:
            job = await uow.processing_jobs.system_get(job_id)
            return None if job is None else job.stage

    async def hand_off(self, job_id: UUID, *, stage: ProcessingStage, progress: float) -> bool:
        """running -> queued at the start of the next stage, so the task for that stage claims
        it like any other. The heartbeat is refreshed, which keeps the sweep away until the job
        has genuinely been waiting."""
        async with self._uow_factory() as uow:
            done = await uow.processing_jobs.system_transition(
                job_id,
                [JobStatus.RUNNING],
                JobStatus.QUEUED,
                stage=stage,
                progress=progress,
                heartbeat_at=datetime.now(UTC),
            )
            await uow.commit()
            return done

    async def release(self, kind: JobKind, job_id: UUID) -> bool:
        """running -> queued before a retry, so the retried delivery can claim the job again.
        Without this a retry would find the job still `running` and do nothing."""
        async with self._uow_factory() as uow:
            repo: Any = uow.processing_jobs if kind == PROCESSING else uow.analysis_jobs
            released: bool = await repo.system_transition(
                job_id, [JobStatus.RUNNING], JobStatus.QUEUED, heartbeat_at=datetime.now(UTC)
            )
            await uow.commit()
            return released

    async def mark_succeeded(self, kind: JobKind, job_id: UUID) -> bool:
        async with self._uow_factory() as uow:
            repo: Any = uow.processing_jobs if kind == PROCESSING else uow.analysis_jobs
            done: bool = await repo.system_transition(
                job_id,
                [JobStatus.RUNNING],
                JobStatus.SUCCEEDED,
                progress=1.0,
                finished_at=datetime.now(UTC),
            )
            await uow.commit()
            return done

    async def mark_failed(self, kind: JobKind, job_id: UUID, *, code: str, message: str) -> bool:
        """Record the final failure. Always called *before* a message is dead-lettered, so
        PostgreSQL shows the true state however the broker behaves (spec 25.6)."""
        now = datetime.now(UTC)
        async with self._uow_factory() as uow:
            if kind == PROCESSING:
                job = await uow.processing_jobs.system_get(job_id)
                if job is None:
                    return False
                failed = await uow.processing_jobs.system_transition(
                    job_id,
                    [JobStatus.QUEUED, JobStatus.RUNNING],
                    JobStatus.FAILED,
                    error_code=code,
                    error_message=message,
                    finished_at=now,
                )
                if failed:
                    await uow.documents.system_set_status(
                        job.document_id,
                        _OPEN_DOCUMENT_STATUSES,
                        DocumentStatus.FAILED,
                        error_code=code,
                    )
            else:
                failed = await uow.analysis_jobs.system_transition(
                    job_id,
                    [JobStatus.QUEUED, JobStatus.RUNNING],
                    JobStatus.FAILED,
                    error_code=code,
                    finished_at=now,
                )
            await uow.commit()
            return failed

    async def reset_for_replay(self, kind: JobKind, job_id: UUID) -> bool:
        """failed -> queued, so a dead-lettered task can be replayed. False if the job is not
        failed (already replayed, or never failed)."""
        async with self._uow_factory() as uow:
            if kind == PROCESSING:
                job = await uow.processing_jobs.system_get(job_id)
                if job is None:
                    return False
                reset = await uow.processing_jobs.system_transition(
                    job_id,
                    [JobStatus.FAILED],
                    JobStatus.QUEUED,
                    error_code=None,
                    error_message=None,
                    finished_at=None,
                    started_at=None,
                    heartbeat_at=None,
                    stage=None,
                    progress=0.0,
                )
                if reset:
                    await uow.documents.system_set_status(
                        job.document_id,
                        [DocumentStatus.FAILED],
                        DocumentStatus.QUEUED,
                        error_code=None,
                    )
            else:
                reset = await uow.analysis_jobs.system_transition(
                    job_id,
                    [JobStatus.FAILED],
                    JobStatus.QUEUED,
                    error_code=None,
                    finished_at=None,
                    heartbeat_at=None,
                    stage=None,
                    progress=0.0,
                )
            await uow.commit()
            return reset

    async def reap_lost(self, *, limit: int = 100) -> list[QueuedJob]:
        """Jobs left `running` by a worker that died (killed, out of memory, deployed over).

        RabbitMQ redelivers the dead worker's message at once, but a heartbeat younger than 10
        minutes still looks alive, so the redelivery is skipped and acknowledged and nothing is
        left to resume the job. This puts such jobs (heartbeat older than 10 minutes) back to
        `queued` so they can be published again, or fails them after repeated losses.
        """
        cutoff = datetime.now(UTC) - STALE_HEARTBEAT
        async with self._uow_factory() as uow:
            processing = await uow.processing_jobs.system_list_stale_running(cutoff, limit)
            analysis = await uow.analysis_jobs.system_list_stale_running(cutoff, limit)
        candidates: list[tuple[QueuedJob, int]] = [
            (QueuedJob(PROCESSING, job_id, stage=stage), attempt)
            for job_id, stage, attempt in processing
        ] + [
            (QueuedJob(ANALYSIS, job_id, job_type), attempt)
            for job_id, job_type, attempt in analysis
        ]

        reaped: list[QueuedJob] = []
        for job, attempt in candidates:
            if attempt >= MAX_LOST_ATTEMPTS:
                await self.mark_failed(
                    job.kind,
                    job.job_id,
                    code=LOST_WORKER_CODE,
                    message="The worker stopped repeatedly while running this job.",
                )
                continue
            async with self._uow_factory() as uow:
                repo: Any = uow.processing_jobs if job.kind == PROCESSING else uow.analysis_jobs
                requeued: bool = await repo.system_requeue_if_stale(job.job_id, cutoff)
                await uow.commit()
            if requeued:
                reaped.append(job)
        return reaped

    async def stale_queued(self, *, older_than: timedelta, limit: int = 100) -> list[QueuedJob]:
        """Jobs still `queued` long after creation: their publish was lost, or the broker
        dropped the message. The sweep re-dispatches them."""
        cutoff = datetime.now(UTC) - older_than
        async with self._uow_factory() as uow:
            processing = await uow.processing_jobs.system_list_queued_before(cutoff, limit)
            analysis = await uow.analysis_jobs.system_list_queued_before(cutoff, limit)
        return [QueuedJob(PROCESSING, job_id, stage=stage) for job_id, stage in processing] + [
            QueuedJob(ANALYSIS, job_id, job_type) for job_id, job_type in analysis
        ]
