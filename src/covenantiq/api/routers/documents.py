from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from covenantiq.api.deps import ContainerDep, CurrentPrincipal, rate_limit
from covenantiq.api.schemas.common import ErrorResponse
from covenantiq.api.schemas.documents import (
    DocumentDetail,
    DocumentOut,
    DocumentPage,
    DocumentStatusOut,
    DocumentUpdate,
    UploadResponse,
)
from covenantiq.api.schemas.jobs import JobError, job_out
from covenantiq.core.enums import ContractType, DocumentStatus

router = APIRouter(
    prefix="/documents",
    tags=["documents"],
    dependencies=[Depends(rate_limit("default"))],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)

UPLOAD_ERRORS: dict[int | str, dict[str, Any]] = {
    200: {"model": UploadResponse, "description": "The same bytes were uploaded before."},
    400: {"model": ErrorResponse, "description": "Unsupported, corrupt or encrypted file."},
    413: {"model": ErrorResponse, "description": "The file exceeds the size limit."},
    429: {"model": ErrorResponse},
}


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=UPLOAD_ERRORS,
    dependencies=[Depends(rate_limit("upload"))],
)
async def upload(
    response: Response,
    principal: CurrentPrincipal,
    container: ContainerDep,
    file: Annotated[UploadFile, File(description="A PDF or DOCX contract.")],
    title: Annotated[str | None, Form(max_length=255)] = None,
    vendor_id: Annotated[UUID | None, Form()] = None,
    contract_type: Annotated[ContractType, Form()] = ContractType.UNKNOWN,
) -> UploadResponse:
    result = await container.document_service.upload(
        principal,
        source=file,
        filename=file.filename,
        title=title,
        vendor_id=vendor_id,
        contract_type=contract_type,
    )
    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    elif result.job is not None:
        response.headers["Location"] = f"/api/v1/jobs/{result.job.id}"
    return UploadResponse(
        document=DocumentOut.model_validate(result.document),
        job=job_out(result.job),
        duplicate=result.duplicate,
    )


@router.get("", response_model=DocumentPage)
async def list_documents(
    principal: CurrentPrincipal,
    container: ContainerDep,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    vendor_id: UUID | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
) -> DocumentPage:
    page = await container.document_service.list(
        principal, status=status_filter, vendor_id=vendor_id, q=q, limit=limit, cursor=cursor
    )
    return DocumentPage(
        items=[DocumentOut.model_validate(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{document_id}", response_model=DocumentDetail)
async def get_document(
    document_id: UUID, principal: CurrentPrincipal, container: ContainerDep
) -> DocumentDetail:
    detail = await container.document_service.get(principal, document_id)
    return DocumentDetail(
        **DocumentOut.model_validate(detail.document).model_dump(),
        job=job_out(detail.job),
        extraction_quality=detail.extraction_quality,
        error_code=detail.error_code,
        metadata=detail.metadata,
    )


@router.get("/{document_id}/status", response_model=DocumentStatusOut)
async def get_document_status(
    document_id: UUID, principal: CurrentPrincipal, container: ContainerDep
) -> DocumentStatusOut:
    dto = await container.document_service.get_status(principal, document_id)
    return DocumentStatusOut(
        document_id=dto.document_id,
        status=dto.status,
        stage=dto.stage,
        progress=dto.progress,
        page_count=dto.page_count,
        chunk_count=dto.chunk_count,
        error=JobError(code=dto.error_code) if dto.error_code else None,
        updated_at=dto.updated_at,
    )


@router.patch("/{document_id}", response_model=DocumentOut)
async def update_document(
    document_id: UUID,
    payload: DocumentUpdate,
    principal: CurrentPrincipal,
    container: ContainerDep,
) -> DocumentOut:
    changes: dict[str, Any] = payload.model_dump(exclude_unset=True)
    updated = await container.document_service.update(principal, document_id, changes)
    return DocumentOut.model_validate(updated)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: UUID, principal: CurrentPrincipal, container: ContainerDep
) -> Response:
    await container.document_service.delete(principal, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
