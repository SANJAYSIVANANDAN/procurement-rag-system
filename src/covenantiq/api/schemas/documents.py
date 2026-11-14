from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from covenantiq.api.schemas.jobs import JobError, JobOut
from covenantiq.core.enums import ContractType, DocumentStatus


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class UploadResponse(BaseModel):
    document: DocumentOut
    job: JobOut | None
    duplicate: bool


class DocumentDetail(DocumentOut):
    job: JobOut | None
    extraction_quality: dict[str, Any]
    error_code: str | None
    # includes injection_warning / injection_flags when the scan flagged the document
    metadata: dict[str, Any]


class DocumentStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    document_id: UUID
    status: DocumentStatus
    stage: str | None
    progress: float
    page_count: int | None
    chunk_count: int
    error: JobError | None
    updated_at: datetime


class DocumentUpdate(BaseModel):
    """Only the fields present in the request are changed; `vendor_id: null` unlinks the vendor."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=255)
    vendor_id: UUID | None = None
    contract_type: ContractType | None = None
    effective_date: date | None = None
    expiry_date: date | None = None


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    next_cursor: str | None
