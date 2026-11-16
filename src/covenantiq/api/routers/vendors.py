from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from covenantiq.api.deps import ContainerDep, CurrentPrincipal, rate_limit
from covenantiq.api.schemas.assessments import (
    AssessJob,
    AssessmentOut,
    AssessResponse,
    DimensionOut,
    DocumentRef,
    MissingInfoOut,
    SignalOut,
)
from covenantiq.api.schemas.common import ErrorResponse
from covenantiq.api.schemas.conversations import CitationOut
from covenantiq.api.schemas.documents import DocumentOut
from covenantiq.api.schemas.jobs import job_to_out
from covenantiq.api.schemas.vendors import (
    VendorCreate,
    VendorDetail,
    VendorOut,
    VendorPage,
    VendorUpdate,
)

router = APIRouter(
    prefix="/vendors",
    tags=["vendors"],
    dependencies=[Depends(rate_limit("default"))],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)


@router.post(
    "",
    response_model=VendorOut,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
)
async def create_vendor(
    payload: VendorCreate, principal: CurrentPrincipal, container: ContainerDep
) -> VendorOut:
    vendor = await container.vendor_service.create(
        principal,
        name=payload.name,
        industry=payload.industry,
        website=payload.website,
        metadata=payload.metadata,
    )
    return VendorOut.model_validate(vendor)


@router.get("", response_model=VendorPage)
async def list_vendors(
    principal: CurrentPrincipal,
    container: ContainerDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
) -> VendorPage:
    page = await container.vendor_service.list(principal, q=q, limit=limit, cursor=cursor)
    return VendorPage(
        items=[VendorOut.model_validate(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{vendor_id}", response_model=VendorDetail)
async def get_vendor(
    vendor_id: UUID, principal: CurrentPrincipal, container: ContainerDep
) -> VendorDetail:
    detail = await container.vendor_service.get(principal, vendor_id)
    return VendorDetail(
        **VendorOut.model_validate(detail.vendor).model_dump(),
        documents=[DocumentOut.model_validate(d) for d in detail.documents],
    )


@router.patch("/{vendor_id}", response_model=VendorOut, responses={409: {"model": ErrorResponse}})
async def update_vendor(
    vendor_id: UUID,
    payload: VendorUpdate,
    principal: CurrentPrincipal,
    container: ContainerDep,
) -> VendorOut:
    changes: dict[str, Any] = payload.model_dump(exclude_unset=True)
    vendor = await container.vendor_service.update(principal, vendor_id, changes)
    return VendorOut.model_validate(vendor)


# ---- assessment ----------------------------------------------------------------------------


def assessment_out(dto: Any) -> AssessmentOut:
    return AssessmentOut(
        id=dto.id,
        vendor_id=dto.vendor_id,
        assessed_at=dto.assessed_at,
        scoring_config_version=dto.scoring_config_version,
        overall_score=dto.overall_score,
        overall_severity=dto.overall_severity,
        evidence_coverage=dto.evidence_coverage,
        completeness=dto.completeness,
        provisional=dto.provisional,
        weights=dto.weights,
        effective_weights=dto.effective_weights,
        documents_considered=[DocumentRef.model_validate(d) for d in dto.documents_considered],
        dimensions=[
            DimensionOut(
                dimension=d.dimension,
                status=d.status,
                score=d.score,
                severity=d.severity,
                coverage=d.coverage,
                reasoning=d.reasoning,
                signals=[SignalOut.model_validate(s) for s in d.signals],
                missing_information=[
                    MissingInfoOut.model_validate(m) for m in d.missing_information
                ],
                mitigations=d.mitigations,
                evidence=[CitationOut.model_validate(e, from_attributes=True) for e in d.evidence],
            )
            for d in dto.dimensions
        ],
        notes=dto.notes,
    )


@router.post(
    "/{vendor_id}/assess",
    response_model=AssessResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("analysis_start"))],
    responses={200: {"model": AssessResponse, "description": "An assessment is already running."}},
)
async def assess_vendor(
    vendor_id: UUID, response: Response, principal: CurrentPrincipal, container: ContainerDep
) -> AssessResponse:
    """Assess a vendor across all its ready documents. Contract analyses the assessment needs are
    started first and listed as `pending_dependencies`. Poll the job at `GET /jobs/{id}`."""
    started = await container.vendor_assessment_service.start(principal, vendor_id)
    if not started.created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/api/v1/jobs/{started.job.id}"
    out = job_to_out(started.job)
    return AssessResponse(
        job=AssessJob(
            **out.model_dump(),
            scoring_config_version=started.scoring_config_version,
            pending_dependencies=started.pending_dependencies,
        )
    )


@router.get("/{vendor_id}/assessments", response_model=list[AssessmentOut])
async def list_assessments(
    vendor_id: UUID,
    principal: CurrentPrincipal,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AssessmentOut]:
    rows = await container.vendor_assessment_service.history(
        principal, vendor_id, limit=limit, offset=offset
    )
    return [assessment_out(r) for r in rows]


@router.get(
    "/{vendor_id}/assessments/latest",
    response_model=AssessmentOut,
    responses={404: {"model": ErrorResponse, "description": "No completed assessment yet"}},
)
async def latest_assessment(
    vendor_id: UUID, principal: CurrentPrincipal, container: ContainerDep
) -> AssessmentOut:
    return assessment_out(await container.vendor_assessment_service.latest(principal, vendor_id))
