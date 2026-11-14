from uuid import UUID

from fastapi import APIRouter, Depends

from covenantiq.api.deps import ContainerDep, CurrentPrincipal, rate_limit
from covenantiq.api.schemas.common import ErrorResponse
from covenantiq.api.schemas.jobs import JobOut, job_to_out

router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(rate_limit("default"))],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: UUID, principal: CurrentPrincipal, container: ContainerDep) -> JobOut:
    """Status of a background job: document processing or an analysis."""
    return job_to_out(await container.job_service.get(principal, job_id))
