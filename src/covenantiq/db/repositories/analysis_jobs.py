from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from covenantiq.core.enums import AnalysisJobType, JobStatus
from covenantiq.db.models import AnalysisJob
from covenantiq.db.repositories.base import Repository, cas_transition, ensure_owned
from covenantiq.db.scope import OwnershipScope

ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)


class AnalysisJobRepository(Repository):
    async def create_or_get_active(
        self, scope: OwnershipScope, job: AnalysisJob
    ) -> tuple[AnalysisJob, bool]:
        """Insert `job`, or return the existing queued/running job with the same idempotency key.

        Returns (job, created). Safe under concurrent callers: the unique index on
        idempotency_key arbitrates and the loser reads the winner's row.
        """
        ensure_owned(scope, job)
        existing = await self._active_by_key(scope, job.idempotency_key)
        if existing is not None:
            return existing, False
        try:
            async with self.session.begin_nested():
                self.session.add(job)
                await self.session.flush()
        except IntegrityError:
            winner = await self._active_by_key(scope, job.idempotency_key)
            if winner is None:
                raise
            return winner, False
        return job, True

    async def _active_by_key(self, scope: OwnershipScope, key: str) -> AnalysisJob | None:
        statement = scope.apply(
            select(AnalysisJob).where(
                AnalysisJob.idempotency_key == key, AnalysisJob.status.in_(ACTIVE_STATUSES)
            ),
            AnalysisJob,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get(self, scope: OwnershipScope, job_id: UUID) -> AnalysisJob | None:
        statement = scope.apply(select(AnalysisJob).where(AnalysisJob.id == job_id), AnalysisJob)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def system_get(self, job_id: UUID) -> AnalysisJob | None:
        return await self.session.get(AnalysisJob, job_id)

    async def system_claim(self, job_id: UUID, stale_before: datetime) -> tuple[UUID, int] | None:
        """queued -> running, or takeover of a running job with a stale heartbeat."""
        statement = (
            update(AnalysisJob)
            .where(
                AnalysisJob.id == job_id,
                or_(
                    AnalysisJob.status == JobStatus.QUEUED,
                    and_(
                        AnalysisJob.status == JobStatus.RUNNING,
                        AnalysisJob.heartbeat_at < stale_before,
                    ),
                ),
            )
            .values(
                status=JobStatus.RUNNING,
                attempt=AnalysisJob.attempt + 1,
                heartbeat_at=func.now(),
            )
            .returning(AnalysisJob.owner_id, AnalysisJob.attempt)
            .execution_options(synchronize_session=False)
        )
        row = (await self.session.execute(statement)).one_or_none()
        return None if row is None else (row.owner_id, row.attempt)

    async def system_touch(self, job_id: UUID, **fields: Any) -> bool:
        statement = (
            update(AnalysisJob)
            .where(AnalysisJob.id == job_id, AnalysisJob.status == JobStatus.RUNNING)
            .values(heartbeat_at=func.now(), **fields)
            .returning(AnalysisJob.id)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def system_list_queued_before(
        self, cutoff: datetime, limit: int
    ) -> Sequence[tuple[UUID, AnalysisJobType]]:
        statement = (
            select(AnalysisJob.id, AnalysisJob.job_type)
            .where(AnalysisJob.status == JobStatus.QUEUED, AnalysisJob.created_at < cutoff)
            .order_by(AnalysisJob.created_at)
            .limit(limit)
        )
        return [(row.id, row.job_type) for row in await self.session.execute(statement)]

    async def system_list_stale_running(
        self, cutoff: datetime, limit: int
    ) -> Sequence[tuple[UUID, AnalysisJobType, int]]:
        """Running jobs whose last heartbeat is older than `cutoff`: their worker is gone."""
        statement = (
            select(AnalysisJob.id, AnalysisJob.job_type, AnalysisJob.attempt)
            .where(AnalysisJob.status == JobStatus.RUNNING, AnalysisJob.heartbeat_at < cutoff)
            .order_by(AnalysisJob.heartbeat_at)
            .limit(limit)
        )
        return [
            (row.id, row.job_type, row.attempt) for row in await self.session.execute(statement)
        ]

    async def system_requeue_if_stale(self, job_id: UUID, cutoff: datetime) -> bool:
        """running -> queued, but only if the heartbeat is still older than `cutoff` (checked in
        the UPDATE itself, so a worker that heartbeats in between keeps its job)."""
        statement = (
            update(AnalysisJob)
            .where(
                AnalysisJob.id == job_id,
                AnalysisJob.status == JobStatus.RUNNING,
                AnalysisJob.heartbeat_at < cutoff,
            )
            .values(status=JobStatus.QUEUED, heartbeat_at=func.now())
            .returning(AnalysisJob.id)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def system_transition(
        self, job_id: UUID, expected: Collection[JobStatus], new: JobStatus, **fields: Any
    ) -> bool:
        return await cas_transition(
            self.session, AnalysisJob, job_id, "status", expected, new, **fields
        )
