from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID

from covenantiq.core.enums import ContractType, DocumentStatus, JobStatus
from covenantiq.db.models import AnalysisJob, Document, DocumentProcessingJob, Vendor
from covenantiq.db.scope import OwnershipScope


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. Services receive this and build an OwnershipScope from it."""

    user_id: UUID
    email: str
    roles: frozenset[str] = frozenset({"owner"})

    def scope(self) -> OwnershipScope:
        return OwnershipScope(self.user_id)


@dataclass(frozen=True)
class UserDTO:
    id: UUID
    email: str
    full_name: str | None
    created_at: datetime


@dataclass(frozen=True)
class TokenDTO:
    access_token: str
    expires_in: int


@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None


@dataclass(frozen=True)
class DocumentDTO:
    id: UUID
    title: str
    status: DocumentStatus
    original_filename: str
    size_bytes: int
    vendor_id: UUID | None
    contract_type: ContractType
    page_count: int | None
    chunk_count: int
    effective_date: date | None
    expiry_date: date | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class JobDTO:
    id: UUID
    kind: str
    status: JobStatus
    stage: str | None
    progress: float
    attempt: int
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class DocumentDetailDTO:
    document: DocumentDTO
    job: JobDTO | None
    extraction_quality: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UploadResultDTO:
    document: DocumentDTO
    job: JobDTO | None
    duplicate: bool


@dataclass(frozen=True)
class DocumentStatusDTO:
    document_id: UUID
    status: DocumentStatus
    stage: str | None
    progress: float
    page_count: int | None
    chunk_count: int
    error_code: str | None
    updated_at: datetime


@dataclass(frozen=True)
class VendorDTO:
    id: UUID
    name: str
    industry: str | None
    website: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class VendorDetailDTO:
    vendor: VendorDTO
    documents: list[DocumentDTO]


def document_to_dto(document: Document) -> DocumentDTO:
    return DocumentDTO(
        id=document.id,
        title=document.title,
        status=document.status,
        original_filename=document.original_filename,
        size_bytes=document.size_bytes,
        vendor_id=document.vendor_id,
        contract_type=document.contract_type,
        page_count=document.page_count,
        chunk_count=document.chunk_count,
        effective_date=document.effective_date,
        expiry_date=document.expiry_date,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def job_to_dto(job: DocumentProcessingJob) -> JobDTO:
    return JobDTO(
        id=job.id,
        kind="document_processing",
        status=job.status,
        stage=job.stage.value if job.stage is not None else None,
        progress=job.progress,
        attempt=job.attempt,
        error_code=job.error_code,
        error_message=job.error_message,
    )


def analysis_job_to_dto(job: AnalysisJob) -> JobDTO:
    return JobDTO(
        id=job.id,
        kind=job.job_type.value,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        attempt=job.attempt,
        error_code=job.error_code,
        error_message=None,
    )


def vendor_to_dto(vendor: Vendor) -> VendorDTO:
    return VendorDTO(
        id=vendor.id,
        name=vendor.name,
        industry=vendor.industry,
        website=vendor.website,
        metadata=vendor.metadata_,
        created_at=vendor.created_at,
        updated_at=vendor.updated_at,
    )
