from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from covenantiq.api.deps import ContainerDep, CurrentPrincipal, rate_limit
from covenantiq.api.schemas.common import ErrorResponse
from covenantiq.api.schemas.findings import (
    AnalyzeJob,
    AnalyzeRequest,
    AnalyzeResponse,
    FindingOut,
    FindingsResponse,
)
from covenantiq.api.schemas.jobs import job_to_out
from covenantiq.core.enums import ClauseStatus, RiskCategory, Severity

router = APIRouter(
    prefix="/documents",
    tags=["analysis"],
    dependencies=[Depends(rate_limit("default"))],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)


@router.post(
    "/{document_id}/analyze",
    response_model=AnalyzeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("analysis_start"))],
    responses={
        200: {"model": AnalyzeResponse, "description": "A running or current analysis exists."},
        409: {"model": ErrorResponse, "description": "The document is not ready"},
    },
)
async def analyze_document(
    document_id: UUID,
    response: Response,
    principal: CurrentPrincipal,
    container: ContainerDep,
    payload: AnalyzeRequest | None = None,
) -> AnalyzeResponse:
    """Start a contract analysis. Poll the job at `GET /jobs/{id}`, then read the findings."""
    request = payload or AnalyzeRequest()
    started = await container.contract_analysis_service.start(
        principal, document_id, ruleset=request.ruleset, force=request.force
    )
    if not started.created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/api/v1/jobs/{started.job.id}"
    out = job_to_out(started.job)
    return AnalyzeResponse(
        job=AnalyzeJob(**out.model_dump(), ruleset_version=started.ruleset_version)
    )


@router.get("/{document_id}/findings", response_model=FindingsResponse)
async def get_findings(
    document_id: UUID,
    principal: CurrentPrincipal,
    container: ContainerDep,
    job_id: Annotated[
        UUID | None, Query(description="Default: the latest succeeded analysis")
    ] = None,
    severity: Severity | None = None,
    clause_status: Annotated[ClauseStatus | None, Query(alias="status")] = None,
    category: RiskCategory | None = None,
) -> FindingsResponse:
    report = await container.contract_analysis_service.findings(
        principal,
        document_id,
        job_id=job_id,
        severity=severity,
        status=clause_status,
        category=category,
    )
    return FindingsResponse(
        document_id=report.document_id,
        job_id=report.job_id,
        ruleset_version=report.ruleset_version,
        analyzed_at=report.analyzed_at,
        summary=report.summary,
        notes=report.notes,
        disclaimer=report.disclaimer,
        findings=[FindingOut.model_validate(f) for f in report.findings],
    )
