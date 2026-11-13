from collections.abc import Collection
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from covenantiq.core.enums import JobStatus
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

    async def system_transition(
        self, job_id: UUID, expected: Collection[JobStatus], new: JobStatus, **fields: Any
    ) -> bool:
        return await cas_transition(
            self.session, AnalysisJob, job_id, "status", expected, new, **fields
        )
