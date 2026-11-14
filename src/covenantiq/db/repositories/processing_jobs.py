from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update

from covenantiq.core.enums import JobStatus, ProcessingStage
from covenantiq.db.models import DocumentProcessingJob
from covenantiq.db.repositories.base import Repository, cas_transition, ensure_owned
from covenantiq.db.scope import OwnershipScope


class ProcessingJobRepository(Repository):
    async def create(
        self, scope: OwnershipScope, job: DocumentProcessingJob
    ) -> DocumentProcessingJob:
        """Raises IntegrityError if the document already has a queued or running job."""
        ensure_owned(scope, job)
        self.session.add(job)
        await self.session.flush()
        return job

    async def get(self, scope: OwnershipScope, job_id: UUID) -> DocumentProcessingJob | None:
        statement = scope.apply(
            select(DocumentProcessingJob).where(DocumentProcessingJob.id == job_id),
            DocumentProcessingJob,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def latest_for_document(
        self, scope: OwnershipScope, document_id: UUID
    ) -> DocumentProcessingJob | None:
        statement = scope.apply(
            select(DocumentProcessingJob)
            .where(DocumentProcessingJob.document_id == document_id)
            .order_by(DocumentProcessingJob.id.desc())
            .limit(1),
            DocumentProcessingJob,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def count_active(self, scope: OwnershipScope) -> int:
        statement = scope.apply(
            select(func.count())
            .select_from(DocumentProcessingJob)
            .where(DocumentProcessingJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])),
            DocumentProcessingJob,
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def system_get(self, job_id: UUID) -> DocumentProcessingJob | None:
        return await self.session.get(DocumentProcessingJob, job_id)

    async def system_claim(self, job_id: UUID, stale_before: datetime) -> tuple[UUID, int] | None:
        """Take ownership of a job: queued -> running, or a takeover of a running job whose
        heartbeat is older than `stale_before` (its worker died). Returns (owner_id, attempt)
        for exactly one caller; everyone else gets None."""
        statement = (
            update(DocumentProcessingJob)
            .where(
                DocumentProcessingJob.id == job_id,
                or_(
                    DocumentProcessingJob.status == JobStatus.QUEUED,
                    and_(
                        DocumentProcessingJob.status == JobStatus.RUNNING,
                        DocumentProcessingJob.heartbeat_at < stale_before,
                    ),
                ),
            )
            .values(
                status=JobStatus.RUNNING,
                attempt=DocumentProcessingJob.attempt + 1,
                started_at=func.coalesce(DocumentProcessingJob.started_at, func.now()),
                heartbeat_at=func.now(),
            )
            .returning(DocumentProcessingJob.owner_id, DocumentProcessingJob.attempt)
            .execution_options(synchronize_session=False)
        )
        row = (await self.session.execute(statement)).one_or_none()
        return None if row is None else (row.owner_id, row.attempt)

    async def system_touch(self, job_id: UUID, **fields: Any) -> bool:
        """Record liveness (and optionally stage/progress) of a running job."""
        statement = (
            update(DocumentProcessingJob)
            .where(
                DocumentProcessingJob.id == job_id,
                DocumentProcessingJob.status == JobStatus.RUNNING,
            )
            .values(heartbeat_at=func.now(), **fields)
            .returning(DocumentProcessingJob.id)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def system_list_queued_before(
        self, cutoff: datetime, limit: int
    ) -> Sequence[tuple[UUID, ProcessingStage | None]]:
        """Queued jobs idle since `cutoff`, with the stage they are waiting to run. Idle time is
        counted from the last heartbeat, so a job handed from one stage to the next is not
        mistaken for a lost one."""
        last_seen = func.coalesce(
            DocumentProcessingJob.heartbeat_at, DocumentProcessingJob.created_at
        )
        statement = (
            select(DocumentProcessingJob.id, DocumentProcessingJob.stage)
            .where(DocumentProcessingJob.status == JobStatus.QUEUED, last_seen < cutoff)
            .order_by(last_seen)
            .limit(limit)
        )
        return [(row.id, row.stage) for row in (await self.session.execute(statement)).all()]

    async def system_list_stale_running(
        self, cutoff: datetime, limit: int
    ) -> Sequence[tuple[UUID, ProcessingStage | None, int]]:
        """Running jobs whose last heartbeat is older than `cutoff`: their worker is gone."""
        statement = (
            select(
                DocumentProcessingJob.id,
                DocumentProcessingJob.stage,
                DocumentProcessingJob.attempt,
            )
            .where(
                DocumentProcessingJob.status == JobStatus.RUNNING,
                DocumentProcessingJob.heartbeat_at < cutoff,
            )
            .order_by(DocumentProcessingJob.heartbeat_at)
            .limit(limit)
        )
        return [(row.id, row.stage, row.attempt) for row in await self.session.execute(statement)]

    async def system_requeue_if_stale(self, job_id: UUID, cutoff: datetime) -> bool:
        """running -> queued, but only if the heartbeat is still older than `cutoff`. The check
        is in the UPDATE itself, so a worker that heartbeats in between keeps its job."""
        statement = (
            update(DocumentProcessingJob)
            .where(
                DocumentProcessingJob.id == job_id,
                DocumentProcessingJob.status == JobStatus.RUNNING,
                DocumentProcessingJob.heartbeat_at < cutoff,
            )
            .values(status=JobStatus.QUEUED, heartbeat_at=func.now())
            .returning(DocumentProcessingJob.id)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def system_transition(
        self, job_id: UUID, expected: Collection[JobStatus], new: JobStatus, **fields: Any
    ) -> bool:
        return await cas_transition(
            self.session, DocumentProcessingJob, job_id, "status", expected, new, **fields
        )
