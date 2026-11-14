from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from covenantiq.core.enums import DocumentStatus, ProcessingStage
from covenantiq.core.logging import get_logger
from covenantiq.db.models import DocumentProcessingJob
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.services.task_dispatcher import TaskDispatcher

log = get_logger(__name__)

# Documents with vectors worth replacing: finished ones, and ones whose embedding stage failed.
REEMBEDDABLE = [DocumentStatus.READY, DocumentStatus.FAILED]
DEFAULT_LIMIT = 1000


@dataclass(frozen=True)
class ReembedResult:
    queued: int
    skipped: int  # not ready or failed, deleted, or already being processed


class ReembedService:
    """Embed documents again, for example after switching embedding model.

    Vectors from different models are not comparable, so every chunk of a document is cleared
    and the document goes back through the embedding stage as a new job. The clauses and chunks
    themselves are kept: nothing is extracted or chunked again.
    """

    def __init__(self, uow_factory: Callable[[], UnitOfWork], dispatcher: TaskDispatcher) -> None:
        self._uow_factory = uow_factory
        self._dispatcher = dispatcher

    async def reembed(
        self, *, document_id: UUID | None = None, limit: int = DEFAULT_LIMIT
    ) -> ReembedResult:
        targets = await self._targets(document_id, limit)
        queued = 0
        for target_id, owner_id in targets:
            job_id = await self._reset(target_id, OwnershipScope(owner_id))
            if job_id is None:
                continue
            await self._dispatcher.enqueue_embedding(job_id)
            queued += 1
        skipped = len(targets) - queued
        if document_id is not None and not targets:
            skipped = 1  # the named document does not exist
        log.info("reembed.queued", queued=queued, skipped=skipped)
        return ReembedResult(queued=queued, skipped=skipped)

    async def _targets(self, document_id: UUID | None, limit: int) -> Sequence[tuple[UUID, UUID]]:
        async with self._uow_factory() as uow:
            if document_id is None:
                return await uow.documents.system_ids_with_status(REEMBEDDABLE, limit)
            document = await uow.documents.system_get(document_id)
            if document is None or document.deleted_at is not None:
                return []
            return [(document.id, document.owner_id)]

    async def _reset(self, document_id: UUID, scope: OwnershipScope) -> UUID | None:
        """Clear the vectors and create the embedding job, in one transaction. None if the
        document is not in a state that can be re-embedded or already has an active job."""
        try:
            async with self._uow_factory() as uow:
                moved = await uow.documents.system_set_status(
                    document_id, REEMBEDDABLE, DocumentStatus.EMBEDDING, error_code=None
                )
                if not moved:
                    return None
                await uow.chunks.clear_embeddings(scope, document_id)
                job = await uow.processing_jobs.create(
                    scope,
                    DocumentProcessingJob(
                        owner_id=scope.owner_id,
                        document_id=document_id,
                        stage=ProcessingStage.EMBEDDING,
                        progress=0.55,
                    ),
                )
                await uow.commit()
                return job.id
        except IntegrityError:
            return None  # an ingestion job is already queued or running for this document
