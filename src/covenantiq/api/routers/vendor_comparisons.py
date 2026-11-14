from uuid import UUID

from fastapi import APIRouter, Depends

from covenantiq.api.deps import ContainerDep, CurrentPrincipal, rate_limit
from covenantiq.api.schemas.assessments import ComparisonOut
from covenantiq.api.schemas.common import ErrorResponse
from covenantiq.api.schemas.jobs import job_to_out

router = APIRouter(
    prefix="/vendor-comparisons",
    tags=["vendors"],
    dependencies=[Depends(rate_limit("default"))],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)


@router.get("/{comparison_id}", response_model=ComparisonOut)
async def get_comparison(
    comparison_id: UUID, principal: CurrentPrincipal, container: ContainerDep
) -> ComparisonOut:
    """The comparison matrix and its summary. `result` is empty until the job has succeeded."""
    dto = await container.vendor_comparison_service.get(principal, comparison_id)
    return ComparisonOut(
        id=dto.id,
        status=dto.job.status,
        job=job_to_out(dto.job),
        vendor_ids=dto.vendor_ids,
        assessment_ids=dto.assessment_ids,
        result=dto.result or None,
        narrative=dto.narrative,
        created_at=dto.created_at,
    )
