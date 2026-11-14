import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from covenantiq.ai.embeddings.base import (
    EmbeddingAuthFailed,
    EmbeddingProvider,
    EmbeddingRejected,
)
from covenantiq.core.enums import DocumentStatus, ProcessingStage
from covenantiq.core.errors import PermanentTaskError
from covenantiq.core.ids import uuid7
from covenantiq.core.logging import get_logger
from covenantiq.db.models import DocumentChunk, DocumentClause
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.ingestion.chunking.chunker import ChunkSettings
from covenantiq.ingestion.chunking.document import ChunkedDocument, chunk_document
from covenantiq.ingestion.extraction.base import DocumentExtractor
from covenantiq.ingestion.extraction.quality import require_text
from covenantiq.ingestion.extraction.types import ExtractedBlock, ExtractionResult
from covenantiq.ingestion.injection_scan import FLAG_THRESHOLD
from covenantiq.ingestion.storage import FileStorage
from covenantiq.ingestion.taxonomy import Taxonomy, get_taxonomy
from covenantiq.services.job_service import PROCESSING, JobService
from covenantiq.services.task_dispatcher import NullTaskDispatcher, TaskDispatcher
from covenantiq.workers.heartbeat import heartbeat

log = get_logger(__name__)

# Progress of the job: extracting runs from EXTRACT_START to EXTRACT_END, chunking ends at
# CHUNKING_PROGRESS, and embedding fills the rest up to 1.0.
EXTRACT_START = 0.05
EXTRACT_END = 0.45
CHUNKING_PROGRESS = 0.5
EMBED_START = 0.55
EMBED_END = 0.95

# A document may be picked up from any of these. A redelivery or a replay finds it part-way
# through; starting again from the top is safe because persisting chunks replaces the old ones.
STARTABLE = [
    DocumentStatus.UPLOADED,
    DocumentStatus.QUEUED,
    DocumentStatus.EXTRACTING,
    DocumentStatus.CHUNKING,
    DocumentStatus.EMBEDDING,
]

# The document is ready when at most this share of its chunks was refused by the provider.
MAX_REFUSED_FRACTION = 0.02
EMBED_ERROR_CODE = "EMBEDDING_FAILED"


@dataclass(frozen=True)
class ChunkingConfig:
    target_tokens: int = 350
    parent_max_tokens: int = 1500
    heading_depth: int | None = None


class IngestionService:
    """What the ingestion workers do.

    `process` extracts the document, builds its clauses and chunks, stores them and hands the job
    on to the embedding stage. `embed` (the same entry point, chosen by the job's stage) embeds
    the chunks in batches, committing as it goes so a retry resumes where it stopped.
    """

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        storage: FileStorage,
        jobs: JobService,
        extractor: DocumentExtractor,
        *,
        embedder: EmbeddingProvider | None = None,
        dispatcher: TaskDispatcher | None = None,
        chunking: ChunkingConfig | None = None,
        taxonomy: Taxonomy | None = None,
        embed_batch_size: int = 64,
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage
        self._jobs = jobs
        self._extractor = extractor
        self._embedder = embedder
        self._dispatcher: TaskDispatcher = dispatcher or NullTaskDispatcher()
        self._chunking = chunking or ChunkingConfig()
        self._taxonomy = taxonomy
        self._embed_batch_size = embed_batch_size

    # ---- entry points -----------------------------------------------------------------

    async def process(self, job_id: UUID, *, attempt: int) -> dict[str, Any]:
        """`ingest.process_document` and `ingest.embed_document`. Safe to deliver twice: only
        the caller that wins the claim does any work, and the job's stage says which half of the
        pipeline is left to run, whichever task name delivered the message."""
        claim = await self._jobs.claim(PROCESSING, job_id)
        if not claim.exists:
            raise PermanentTaskError("The processing job does not exist.", job_code="JOB_NOT_FOUND")
        if not claim.claimed:
            log.info("ingest.skipped", job_id=str(job_id), reason="not claimable")
            return {"status": "skipped"}

        if await self._jobs.stage_of(job_id) == ProcessingStage.EMBEDDING:
            return await self._embed(job_id)
        return await self._extract_and_chunk(job_id, attempt)

    embed = process

    # ---- extraction and chunking --------------------------------------------------------

    async def _extract_and_chunk(self, job_id: UUID, attempt: int) -> dict[str, Any]:
        document_id, owner_id, title, storage_path = await self._begin_extraction(job_id)
        extension = Path(storage_path).suffix.lstrip(".").lower()

        result = await self._extract(job_id, storage_path, extension)
        require_text(result.quality)  # a document with no text fails with NO_EXTRACTABLE_TEXT

        await self._record_extraction(document_id, result)
        await self._jobs.touch(PROCESSING, job_id, stage="chunking", progress=CHUNKING_PROGRESS)
        chunked = await asyncio.to_thread(self._chunk, result.blocks, title)
        persisted = await self._persist(document_id, OwnershipScope(owner_id), chunked)
        if not persisted:
            # The document changed state under us (deleted, for example): nothing left to do.
            await self._jobs.mark_succeeded(PROCESSING, job_id)
            return {"status": "abandoned"}

        await self._hand_off_to_embedding(job_id)
        log.info(
            "ingest.chunked",
            job_id=str(job_id),
            attempt=attempt,
            blocks=len(result.blocks),
            clauses=len(chunked.clauses),
            chunks=len(chunked.chunks),
            insufficient_text=result.quality.insufficient_text,
        )
        return {
            "status": "chunked",
            "clauses": len(chunked.clauses),
            "chunks": len(chunked.chunks),
            "insufficient_text": result.quality.insufficient_text,
        }

    def _chunk(self, blocks: list[ExtractedBlock], title: str) -> ChunkedDocument:
        return chunk_document(
            blocks,
            title=title,
            taxonomy=self._taxonomy or get_taxonomy(),
            parent_max_tokens=self._chunking.parent_max_tokens,
            heading_depth=self._chunking.heading_depth,
            settings=ChunkSettings(target_tokens=self._chunking.target_tokens),
        )

    async def _begin_extraction(self, job_id: UUID) -> tuple[UUID, UUID, str, str]:
        async with self._uow_factory() as uow:
            job = await uow.processing_jobs.system_get(job_id)
            if job is None:
                raise PermanentTaskError(
                    "The processing job does not exist.", job_code="JOB_NOT_FOUND"
                )
            document = await uow.documents.system_get(job.document_id)
            if document is None or document.deleted_at is not None:
                raise PermanentTaskError("The document no longer exists.", job_code="DOCUMENT_GONE")
            await uow.documents.system_set_status(document.id, STARTABLE, DocumentStatus.EXTRACTING)
            await uow.commit()
            details = (document.id, document.owner_id, document.title, document.storage_path)
        await self._jobs.touch(PROCESSING, job_id, stage="extracting", progress=EXTRACT_START)
        return details

    async def _extract(self, job_id: UUID, storage_path: str, extension: str) -> ExtractionResult:
        """Run the CPU-bound extractor in a thread, keeping the heartbeat and progress flowing."""
        loop = asyncio.get_running_loop()

        def report(fraction: float) -> None:
            progress = EXTRACT_START + (EXTRACT_END - EXTRACT_START) * fraction
            # called from the extractor's thread; best effort, never blocks or fails extraction
            asyncio.run_coroutine_threadsafe(
                self._jobs.touch(PROCESSING, job_id, progress=progress), loop
            )

        local_file = self._storage.local_path(storage_path)
        async with heartbeat(self._jobs, PROCESSING, job_id):
            return await asyncio.to_thread(
                self._extractor.extract, local_file, extension, progress=report
            )

    async def _record_extraction(self, document_id: UUID, result: ExtractionResult) -> None:
        flagged = [b for b in result.blocks if b.injection_score >= FLAG_THRESHOLD]
        async with self._uow_factory() as uow:
            document = await uow.documents.system_get(document_id)
            if document is None:
                return
            metadata = dict(document.metadata_)
            if flagged:
                metadata["injection_warning"] = True
                metadata["injection_flags"] = sorted(
                    {f for b in flagged for f in b.injection_flags}
                )
            recorded = await uow.documents.system_set_status(
                document_id,
                [DocumentStatus.EXTRACTING],
                DocumentStatus.CHUNKING,
                extraction_quality=result.quality.to_dict(),
                metadata_=metadata,
                page_count=result.page_count
                if result.page_count is not None
                else document.page_count,
            )
            await uow.commit()
        if not recorded:
            # The document changed state while it was being extracted (for example it was deleted).
            log.warning("ingest.result_not_recorded", document_id=str(document_id))

    async def _persist(
        self, document_id: UUID, scope: OwnershipScope, chunked: ChunkedDocument
    ) -> bool:
        """Replace the document's clauses and chunks and move it to `embedding`, atomically."""
        clauses = [
            DocumentClause(
                id=uuid7(),
                owner_id=scope.owner_id,
                document_id=document_id,
                ordinal=draft.ordinal,
                clause_ref=draft.clause_ref,
                heading=draft.heading,
                heading_path=list(draft.heading_path),
                clause_types=list(draft.clause_types),
                page_start=draft.page_start,
                page_end=draft.page_end,
                text=draft.text,
                has_table=draft.has_table,
                metadata_={},
            )
            for draft in chunked.clauses
        ]
        clause_ids = {clause.ordinal: clause.id for clause in clauses}
        clause_types = {draft.ordinal: list(draft.clause_types) for draft in chunked.clauses}
        chunks = [
            DocumentChunk(
                id=uuid7(),
                owner_id=scope.owner_id,
                document_id=document_id,
                clause_id=clause_ids[draft.clause_ordinal],
                ordinal=draft.ordinal,
                text=draft.text,
                token_count=draft.token_count,
                page_start=draft.page_start,
                page_end=draft.page_end,
                clause_types=clause_types[draft.clause_ordinal],
                injection_score=draft.injection_score,
                metadata_={
                    "heading_text": draft.header,
                    "char_start": draft.char_start,
                    "char_end": draft.char_end,
                    "has_table": draft.has_table,
                    "header_repeated": draft.header_repeated,
                    "injection_flags": list(draft.injection_flags),
                },
            )
            for draft in chunked.chunks
        ]
        async with self._uow_factory() as uow:
            moved = await uow.documents.system_set_status(
                document_id,
                [DocumentStatus.CHUNKING],
                DocumentStatus.EMBEDDING,
                chunk_count=len(chunks),
            )
            if not moved:
                return False
            await uow.clauses.replace_for_document(scope, document_id, clauses)
            await uow.chunks.bulk_insert(scope, chunks)
            await uow.commit()
        return True

    async def _hand_off_to_embedding(self, job_id: UUID) -> None:
        """running -> queued at the embedding stage, then announce it. If the announcement is
        lost the job stays queued and the sweep re-publishes it as an embedding task."""
        await self._jobs.hand_off(job_id, stage=ProcessingStage.EMBEDDING, progress=EMBED_START)
        try:
            await self._dispatcher.enqueue_embedding(job_id)
        except Exception:
            log.warning("ingest.embed_dispatch_failed", job_id=str(job_id))

    # ---- embedding ----------------------------------------------------------------------

    async def _embed(self, job_id: UUID) -> dict[str, Any]:
        self._require_embedder()
        document_id, owner_id = await self._document_of(job_id)
        scope = OwnershipScope(owner_id)

        async with heartbeat(self._jobs, PROCESSING, job_id):
            while True:
                batch = await self._next_batch(scope, document_id)
                if not batch:
                    break
                await self._embed_texts(scope, batch)
                total, embedded, refused = await self._counts(scope, document_id)
                fraction = (embedded + refused) / total if total else 1.0
                await self._jobs.touch(
                    PROCESSING,
                    job_id,
                    stage="embedding",
                    progress=EMBED_START + (EMBED_END - EMBED_START) * fraction,
                )

        total, embedded, refused = await self._counts(scope, document_id)
        if total == 0 or refused / total > MAX_REFUSED_FRACTION:
            raise PermanentTaskError(
                "Too many passages could not be embedded."
                if total
                else "The document has no text.",
                job_code=EMBED_ERROR_CODE,
            )
        await self._finish(document_id, job_id, embedded=embedded, refused=refused)
        log.info("ingest.embedded", job_id=str(job_id), chunks=embedded, refused=refused)
        return {"status": "ready", "chunks": embedded, "refused": refused}

    def _require_embedder(self) -> EmbeddingProvider:
        if self._embedder is None:
            raise PermanentTaskError(
                "No embedding provider is configured.", job_code="EMBEDDING_NOT_CONFIGURED"
            )
        return self._embedder

    async def _document_of(self, job_id: UUID) -> tuple[UUID, UUID]:
        async with self._uow_factory() as uow:
            job = await uow.processing_jobs.system_get(job_id)
            if job is None:
                raise PermanentTaskError(
                    "The processing job does not exist.", job_code="JOB_NOT_FOUND"
                )
            document = await uow.documents.system_get(job.document_id)
            if document is None or document.deleted_at is not None:
                raise PermanentTaskError("The document no longer exists.", job_code="DOCUMENT_GONE")
            return document.id, document.owner_id

    async def _next_batch(self, scope: OwnershipScope, document_id: UUID) -> list[tuple[UUID, str]]:
        async with self._uow_factory() as uow:
            rows = await uow.chunks.pending_embedding_batch(
                scope, document_id, self._embed_batch_size
            )
            return [(row.id, self._embedding_text(row)) for row in rows]

    @staticmethod
    def _embedding_text(chunk: DocumentChunk) -> str:
        """The passage with the place it came from in front: `Title | Heading | clause 4.1`."""
        header = chunk.metadata_.get("heading_text")
        return f"{header}\n\n{chunk.text}" if header else chunk.text

    async def _counts(self, scope: OwnershipScope, document_id: UUID) -> tuple[int, int, int]:
        async with self._uow_factory() as uow:
            return await uow.chunks.embedding_counts(scope, document_id)

    async def _embed_texts(self, scope: OwnershipScope, batch: Sequence[tuple[UUID, str]]) -> None:
        """Embed and store a batch. If the provider rejects it, halve it until the offending
        passage is isolated and recorded; the rest are stored normally."""
        embedder = self._require_embedder()
        try:
            vectors = await embedder.embed_documents([text for _, text in batch])
        except EmbeddingAuthFailed as exc:
            raise PermanentTaskError(
                "The embedding provider rejected the API key.", job_code="EMBEDDING_AUTH_FAILED"
            ) from exc
        except EmbeddingRejected as exc:
            if len(batch) == 1:
                log.warning("ingest.chunk_refused", chunk_id=str(batch[0][0]))
                async with self._uow_factory() as uow:
                    await uow.chunks.mark_embedding_error(scope, batch[0][0], str(exc))
                    await uow.commit()
                return
            middle = len(batch) // 2
            await self._embed_texts(scope, batch[:middle])
            await self._embed_texts(scope, batch[middle:])
            return
        async with self._uow_factory() as uow:
            await uow.chunks.set_embeddings(
                scope,
                [(chunk_id, vector) for (chunk_id, _), vector in zip(batch, vectors, strict=True)],
                embedder.model,
            )
            await uow.commit()

    async def _finish(
        self, document_id: UUID, job_id: UUID, *, embedded: int, refused: int
    ) -> None:
        model = self._require_embedder().model
        async with self._uow_factory() as uow:
            document = await uow.documents.system_get(document_id)
            metadata = dict(document.metadata_) if document is not None else {}
            metadata["embedding_model"] = model
            if refused:
                metadata["chunks_without_embedding"] = refused
            await uow.documents.system_set_status(
                document_id,
                [DocumentStatus.EMBEDDING],
                DocumentStatus.READY,
                error_code=None,
                metadata_=metadata,
            )
            await uow.commit()
        await self._jobs.mark_succeeded(PROCESSING, job_id)

    # ---- purge --------------------------------------------------------------------------

    async def purge(self, document_id: UUID) -> dict[str, Any]:
        """`ingest.purge_document`: hard-delete a soft-deleted document's clauses, chunks and
        file. A live document is never touched."""
        async with self._uow_factory() as uow:
            document = await uow.documents.system_get(document_id)
            if document is None or document.deleted_at is None:
                return {"status": "skipped"}
            scope = OwnershipScope(document.owner_id)
            chunks = await uow.chunks.delete_for_document(scope, document_id)
            await uow.clauses.replace_for_document(scope, document_id, [])
            await uow.commit()
            storage_path = document.storage_path
        self._storage.delete(storage_path)
        log.info("ingest.purged", document_id=str(document_id), chunks=chunks)
        return {"status": "purged", "chunks": chunks}
