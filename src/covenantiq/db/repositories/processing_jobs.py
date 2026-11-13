from collections.abc import Collection
from typing import Any
from uuid import UUID

from sqlalchemy import select

from covenantiq.core.enums import JobStatus
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

    async def system_get(self, job_id: UUID) -> DocumentProcessingJob | None:
        return await self.session.get(DocumentProcessingJob, job_id)

    async def system_transition(
        self, job_id: UUID, expected: Collection[JobStatus], new: JobStatus, **fields: Any
    ) -> bool:
        return await cas_transition(
            self.session, DocumentProcessingJob, job_id, "status", expected, new, **fields
        )
