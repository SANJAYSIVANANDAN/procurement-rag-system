import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from covenantiq.core.enums import ContractType, DocumentStatus, JobStatus
from covenantiq.core.errors import (
    DispatchFailed,
    FileTooLarge,
    IngestionBacklog,
    NotFound,
    TooManyActiveJobs,
    UnsupportedFileType,
    ValidationFailed,
)
from covenantiq.core.ids import uuid7
from covenantiq.core.logging import get_logger
from covenantiq.db.models import Document, DocumentProcessingJob
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.ingestion.storage import FileStorage
from covenantiq.ingestion.validation import (
    SNIFF_BYTES,
    check_content_type,
    file_extension,
    probe_structure,
    sanitize_filename,
)
from covenantiq.services.dto import (
    DocumentDetailDTO,
    DocumentDTO,
    DocumentStatusDTO,
    Page,
    Principal,
    UploadResultDTO,
    document_to_dto,
    job_to_dto,
)
from covenantiq.services.pagination import decode_cursor, encode_cursor
from covenantiq.services.task_dispatcher import TaskDispatcher

log = get_logger(__name__)

CHUNK_SIZE = 1024 * 1024
MAX_TITLE_LENGTH = 255
PATCHABLE_FIELDS = frozenset(
    {"title", "vendor_id", "contract_type", "effective_date", "expiry_date"}
)


class AsyncByteSource(Protocol):
    """What the upload reads from: Starlette's UploadFile satisfies this."""

    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True)
class _ReceivedFile:
    path: Path
    size: int
    sha256: str
    mime_type: str


class DocumentService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        storage: FileStorage,
        dispatcher: TaskDispatcher,
        *,
        max_upload_bytes: int,
        max_pages: int,
        max_active_jobs: int,
        backlog: Callable[[], Awaitable[int]] | None = None,
        max_backlog: int = 200,
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage
        self._dispatcher = dispatcher
        self._max_upload_bytes = max_upload_bytes
        self._max_pages = max_pages
        self._max_active_jobs = max_active_jobs
        self._backlog = backlog
        self._max_backlog = max_backlog

    # ---- upload -------------------------------------------------------------------------

    async def upload(
        self,
        principal: Principal,
        *,
        source: AsyncByteSource,
        filename: str | None,
        title: str | None = None,
        vendor_id: UUID | None = None,
        contract_type: ContractType = ContractType.UNKNOWN,
    ) -> UploadResultDTO:
        """Validate, store and record one file (spec 15.8 "Upload"). Re-uploading identical
        bytes returns the existing document with duplicate=True instead of a second copy."""
        scope = principal.scope()
        original_filename = sanitize_filename(filename)
        extension = file_extension(original_filename)
        document_title = self._clean_title(title) if title else Path(original_filename).stem

        await self._check_can_accept(scope, vendor_id)
        received = await self._receive(source, extension)
        try:
            structure = await asyncio.to_thread(
                probe_structure, received.path, extension, max_pages=self._max_pages
            )
            existing = await self._find_duplicate(scope, received.sha256)
        except BaseException:
            received.path.unlink(missing_ok=True)
            raise
        if existing is not None:
            received.path.unlink(missing_ok=True)
            return existing

        document_id = uuid7()
        stored_path = self._storage.put(
            scope.owner_id, document_id, received.sha256, extension, received.path
        )
        try:
            async with self._uow_factory() as uow:
                document = await uow.documents.add(
                    scope,
                    Document(
                        id=document_id,
                        owner_id=scope.owner_id,
                        vendor_id=vendor_id,
                        title=document_title,
                        original_filename=original_filename,
                        mime_type=received.mime_type,
                        size_bytes=received.size,
                        sha256=received.sha256,
                        storage_path=stored_path,
                        status=DocumentStatus.QUEUED,
                        contract_type=contract_type,
                        page_count=structure.page_count,
                    ),
                )
                job = await uow.processing_jobs.create(
                    scope,
                    DocumentProcessingJob(owner_id=scope.owner_id, document_id=document_id),
                )
                await uow.commit()
                document_dto, job_dto = document_to_dto(document), job_to_dto(job)
        except IntegrityError:
            # Two identical uploads raced and the other one committed first.
            self._storage.delete(stored_path)
            winner = await self._find_duplicate(scope, received.sha256)
            if winner is None:
                raise
            return winner
        except BaseException:
            self._storage.delete(stored_path)
            raise

        await self._dispatch_ingestion(scope, document_dto.id, job_dto.id)
        log.info("document.uploaded", document_id=str(document_dto.id), size_bytes=received.size)
        return UploadResultDTO(document=document_dto, job=job_dto, duplicate=False)

    async def _check_can_accept(self, scope: OwnershipScope, vendor_id: UUID | None) -> None:
        if self._backlog is not None and await self._backlog() > self._max_backlog:
            raise IngestionBacklog(
                "Documents are queueing faster than they are processed. Try again shortly.",
                details={"max_backlog": self._max_backlog},
            )
        async with self._uow_factory() as uow:
            if await uow.processing_jobs.count_active(scope) >= self._max_active_jobs:
                raise TooManyActiveJobs(
                    "Too many documents are still being processed. Try again shortly.",
                    details={"max_active_jobs": self._max_active_jobs},
                )
            if vendor_id is not None and await uow.vendors.get(scope, vendor_id) is None:
                raise NotFound("Vendor not found.")

    async def _receive(self, source: AsyncByteSource, extension: str) -> _ReceivedFile:
        """Stream the upload to a temp file in 1 MiB steps, hashing as it goes. The size limit
        is enforced here, while reading, rather than trusting any framework buffering."""
        path = self._storage.temp_path()
        digest = hashlib.sha256()
        size = 0
        head = bytearray()
        mime_type: str | None = None
        try:
            with path.open("wb") as out:
                while chunk := await source.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > self._max_upload_bytes:
                        limit_mb = self._max_upload_bytes // (1024 * 1024)
                        raise FileTooLarge(
                            f"The file is larger than {limit_mb} MB.",
                            details={"max_bytes": self._max_upload_bytes},
                        )
                    digest.update(chunk)
                    if mime_type is None:
                        head += chunk[: SNIFF_BYTES - len(head)]
                        if len(head) >= SNIFF_BYTES:
                            mime_type = check_content_type(extension, bytes(head))
                    await asyncio.to_thread(out.write, chunk)
            if size == 0:
                raise UnsupportedFileType("The file is empty.")
            if mime_type is None:  # a file smaller than the sniff window
                mime_type = check_content_type(extension, bytes(head))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return _ReceivedFile(path=path, size=size, sha256=digest.hexdigest(), mime_type=mime_type)

    async def _find_duplicate(self, scope: OwnershipScope, sha256: str) -> UploadResultDTO | None:
        async with self._uow_factory() as uow:
            existing = await uow.documents.find_active_by_sha(scope, sha256)
            if existing is None:
                return None
            job = await uow.processing_jobs.latest_for_document(scope, existing.id)
            revive = (
                job is not None
                and existing.status is DocumentStatus.FAILED
                and existing.error_code == "DISPATCH_FAILED"
            )
            if revive and job is not None:
                # The earlier upload was stored but never published. Retrying the same file
                # should schedule it, not hand back a document that can never finish.
                await uow.processing_jobs.system_transition(
                    job.id,
                    [JobStatus.FAILED],
                    JobStatus.QUEUED,
                    error_code=None,
                    error_message=None,
                    finished_at=None,
                )
                await uow.documents.system_set_status(
                    existing.id, [DocumentStatus.FAILED], DocumentStatus.QUEUED, error_code=None
                )
                await uow.commit()
                await uow.session.refresh(existing)
                await uow.session.refresh(job)
            result = UploadResultDTO(
                document=document_to_dto(existing),
                job=job_to_dto(job) if job is not None else None,
                duplicate=True,
            )
        if revive and job is not None:
            await self._dispatch_ingestion(scope, existing.id, job.id)
        return result

    async def _dispatch_ingestion(
        self, scope: OwnershipScope, document_id: UUID, job_id: UUID
    ) -> None:
        try:
            await self._dispatcher.enqueue_ingestion(job_id)
        except Exception as exc:
            async with self._uow_factory() as uow:
                await uow.processing_jobs.system_transition(
                    job_id,
                    [JobStatus.QUEUED],
                    JobStatus.FAILED,
                    error_code="DISPATCH_FAILED",
                    error_message="Processing could not be scheduled.",
                    finished_at=datetime.now(UTC),
                )
                await uow.documents.system_set_status(
                    document_id,
                    [DocumentStatus.QUEUED],
                    DocumentStatus.FAILED,
                    error_code="DISPATCH_FAILED",
                )
                await uow.commit()
            raise DispatchFailed("Document processing could not be scheduled. Try again.") from exc

    # ---- read ---------------------------------------------------------------------------

    async def list(
        self,
        principal: Principal,
        *,
        status: DocumentStatus | None = None,
        vendor_id: UUID | None = None,
        q: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[DocumentDTO]:
        after: tuple[datetime, UUID] | None = None
        if cursor is not None:
            created_at, document_id = decode_cursor(cursor, parts=2)
            try:
                after = (datetime.fromisoformat(created_at), UUID(document_id))
            except ValueError as exc:
                raise ValidationFailed("The cursor is invalid.") from exc

        async with self._uow_factory() as uow:
            rows = await uow.documents.list(
                principal.scope(),
                status=status,
                vendor_id=vendor_id,
                q=q,
                limit=limit + 1,
                after=after,
            )
        page = rows[:limit]
        next_cursor = (
            encode_cursor(page[-1].created_at.isoformat(), str(page[-1].id))
            if len(rows) > limit
            else None
        )
        return Page(items=[document_to_dto(row) for row in page], next_cursor=next_cursor)

    async def get(self, principal: Principal, document_id: UUID) -> DocumentDetailDTO:
        scope = principal.scope()
        async with self._uow_factory() as uow:
            document = await uow.documents.get(scope, document_id)
            if document is None:
                raise NotFound("Document not found.")
            job = await uow.processing_jobs.latest_for_document(scope, document_id)
            return DocumentDetailDTO(
                document=document_to_dto(document),
                job=job_to_dto(job) if job is not None else None,
                extraction_quality=document.extraction_quality,
                error_code=document.error_code,
                metadata=document.metadata_,
            )

    async def get_status(self, principal: Principal, document_id: UUID) -> DocumentStatusDTO:
        """The lightweight view the UI polls while a document is processing."""
        scope = principal.scope()
        async with self._uow_factory() as uow:
            document = await uow.documents.get(scope, document_id)
            if document is None:
                raise NotFound("Document not found.")
            job = await uow.processing_jobs.latest_for_document(scope, document_id)
            progress = (
                1.0
                if document.status is DocumentStatus.READY
                else (job.progress if job is not None else 0.0)
            )
            return DocumentStatusDTO(
                document_id=document.id,
                status=document.status,
                stage=job.stage.value if job is not None and job.stage is not None else None,
                progress=progress,
                page_count=document.page_count,
                chunk_count=document.chunk_count,
                error_code=document.error_code or (job.error_code if job is not None else None),
                updated_at=document.updated_at,
            )

    # ---- change -------------------------------------------------------------------------

    async def update(
        self, principal: Principal, document_id: UUID, changes: dict[str, Any]
    ) -> DocumentDTO:
        """Apply only the fields the caller sent; anything else is ignored."""
        scope = principal.scope()
        fields = {name: value for name, value in changes.items() if name in PATCHABLE_FIELDS}
        for required in ("title", "contract_type"):
            if required in fields and fields[required] is None:
                raise ValidationFailed(f"{required} cannot be null.")
        if "title" in fields:
            fields["title"] = self._clean_title(fields["title"])
        async with self._uow_factory() as uow:
            document = await uow.documents.get(scope, document_id)
            if document is None:
                raise NotFound("Document not found.")
            self._check_dates(
                fields.get("effective_date", document.effective_date),
                fields.get("expiry_date", document.expiry_date),
            )
            if fields.get("vendor_id") is not None and (
                await uow.vendors.get(scope, fields["vendor_id"]) is None
            ):
                raise NotFound("Vendor not found.")
            updated = await uow.documents.update(scope, document_id, **fields)
            if updated is None:  # deleted between the read and the write
                raise NotFound("Document not found.")
            await uow.commit()
            return document_to_dto(updated)

    async def delete(self, principal: Principal, document_id: UUID) -> None:
        """Soft delete: hidden from every query at once; files and chunks are purged later."""
        async with self._uow_factory() as uow:
            if not await uow.documents.soft_delete(principal.scope(), document_id):
                raise NotFound("Document not found.")
            await uow.commit()
        try:
            await self._dispatcher.enqueue_purge(document_id)
        except Exception:
            # The document is already hidden; the purge sweep (M6) will pick it up.
            log.warning("document.purge_dispatch_failed", document_id=str(document_id))

    # ---- helpers ------------------------------------------------------------------------

    @staticmethod
    def _clean_title(title: str) -> str:
        cleaned = " ".join(title.split())
        if not cleaned:
            raise ValidationFailed("The title must not be empty.")
        if len(cleaned) > MAX_TITLE_LENGTH:
            raise ValidationFailed(f"The title must be at most {MAX_TITLE_LENGTH} characters.")
        return cleaned

    @staticmethod
    def _check_dates(effective: date | None, expiry: date | None) -> None:
        if effective is not None and expiry is not None and expiry < effective:
            raise ValidationFailed("expiry_date must not be before effective_date.")
