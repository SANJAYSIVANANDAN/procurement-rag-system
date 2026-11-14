"""Vendor risk assessment from the outside in (spec 21, 25.5): start, run on a worker, read."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from covenantiq.analysis.contract.rules_loader import Ruleset
from covenantiq.analysis.vendor.config_schema import VendorConfig
from covenantiq.core.enums import (
    AnalysisJobType,
    DimensionStatus,
    DocumentStatus,
    JobStatus,
    RiskDimension,
    Severity,
)
from covenantiq.core.errors import (
    DispatchFailed,
    NotFound,
    PermanentTaskError,
    ValidationFailed,
)
from covenantiq.core.logging import get_logger
from covenantiq.db.models import AnalysisJob, Citation, VendorRiskAssessment
from covenantiq.db.uow import UnitOfWork
from covenantiq.graphs.vendor_assessment.runner import VendorAssessmentRunner
from covenantiq.services.contract_analysis_service import (
    ContractAnalysisService,
    EvidenceDTO,
)
from covenantiq.services.dto import JobDTO, Principal, analysis_job_to_dto
from covenantiq.services.job_service import ANALYSIS, JobService
from covenantiq.services.task_dispatcher import TaskDispatcher
from covenantiq.workers.heartbeat import heartbeat

log = get_logger(__name__)

DEPENDENCY_WAIT_SECONDS = 30
MAX_DEPENDENCY_WAITS = 20
_ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING)


@dataclass(frozen=True)
class StartedAssessment:
    job: JobDTO
    scoring_config_version: str | None
    pending_dependencies: list[str]
    created: bool


@dataclass(frozen=True)
class DimensionDTO:
    dimension: RiskDimension
    status: DimensionStatus
    score: float | None
    severity: Severity
    coverage: float
    reasoning: str | None
    signals: list[dict[str, Any]]
    missing_information: list[dict[str, Any]]
    mitigations: list[str]
    evidence: list[EvidenceDTO] = field(default_factory=list)


@dataclass(frozen=True)
class AssessmentDTO:
    id: UUID
    vendor_id: UUID
    assessed_at: datetime
    scoring_config_version: str
    overall_score: float | None
    overall_severity: Severity
    evidence_coverage: float
    completeness: float
    provisional: bool
    weights: dict[str, float]
    effective_weights: dict[str, float]
    documents_considered: list[dict[str, Any]]
    dimensions: list[DimensionDTO]
    notes: list[str] = field(default_factory=list)


def effective_weights_of(weights: dict[str, float], assessed: list[str]) -> dict[str, float]:
    """The configured weights renormalised over the dimensions that were assessed."""
    total = sum(weights.get(d, 0.0) for d in assessed)
    return {d: weights[d] / total for d in assessed if d in weights} if total else {}


def assessment_to_dto(
    assessment: VendorRiskAssessment,
    citations: list[Citation],
    notes: list[str] | None = None,
) -> AssessmentDTO:
    by_dimension: dict[UUID, list[Citation]] = {}
    for citation in citations:
        if citation.risk_dimension_id is not None:
            by_dimension.setdefault(citation.risk_dimension_id, []).append(citation)
    order = {d.value: i for i, d in enumerate(RiskDimension)}
    dimensions = sorted(assessment.dimensions, key=lambda d: order.get(d.dimension.value, 99))
    assessed = [d.dimension.value for d in dimensions if d.status is DimensionStatus.ASSESSED]
    return AssessmentDTO(
        id=assessment.id,
        vendor_id=assessment.vendor_id,
        assessed_at=assessment.assessed_at,
        scoring_config_version=assessment.scoring_config_version,
        overall_score=assessment.overall_score,
        overall_severity=assessment.overall_severity,
        evidence_coverage=assessment.evidence_coverage,
        completeness=assessment.completeness,
        provisional=assessment.provisional,
        weights=dict(assessment.weights),
        effective_weights=effective_weights_of(dict(assessment.weights), assessed),
        documents_considered=list(assessment.documents_considered),
        dimensions=[
            DimensionDTO(
                dimension=d.dimension,
                status=d.status,
                score=d.score,
                severity=d.severity,
                coverage=d.coverage,
                reasoning=d.reasoning,
                signals=list(d.signals),
                missing_information=list(d.missing_information),
                mitigations=[str(m) for m in d.mitigations],
                evidence=[
                    EvidenceDTO(
                        marker=c.marker,
                        quote=c.quote,
                        document_id=c.document_id,
                        document_title=c.document_title,
                        page_start=c.page_start,
                        page_end=c.page_end,
                        clause_ref=c.clause_ref,
                        validated=c.validated,
                    )
                    for c in by_dimension.get(d.id, [])
                ],
            )
            for d in dimensions
        ],
        notes=notes or [],
    )


class VendorAssessmentService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        dispatcher: TaskDispatcher,
        jobs: JobService,
        contract: ContractAnalysisService,
        ruleset: Ruleset,
        config: VendorConfig,
        runner: VendorAssessmentRunner | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._dispatcher = dispatcher
        self._jobs = jobs
        self._contract = contract
        self._ruleset = ruleset
        self._config = config
        self._runner = runner

    # ---- start (API process) ----------------------------------------------------------------

    async def start(self, principal: Principal, vendor_id: UUID) -> StartedAssessment:
        """Make sure every ready document has a current contract analysis (starting those that
        do not), then queue the assessment with those analyses as its dependencies (spec 25.5)."""
        scope = principal.scope()
        async with self._uow_factory() as uow:
            if await uow.vendors.get(scope, vendor_id) is None:
                raise NotFound("Vendor not found.")
            running = await uow.analysis_jobs.active_for_vendor(
                scope, vendor_id, AnalysisJobType.VENDOR_ASSESSMENT
            )
            if running is not None:
                return StartedAssessment(
                    analysis_job_to_dto(running),
                    running.scoring_config_version,
                    _pending(running.params),
                    created=False,
                )
            documents = await uow.documents.list(
                scope, status=DocumentStatus.READY, vendor_id=vendor_id, limit=200
            )
        if not documents:
            raise ValidationFailed("The vendor has no ready documents to assess.")

        depends_on: list[UUID] = []
        for document in sorted(documents, key=lambda d: str(d.id)):
            analysis = await self._contract.start(principal, document.id)
            if analysis.job.status in _ACTIVE:
                depends_on.append(analysis.job.id)

        async with self._uow_factory() as uow:
            generation = await uow.analysis_jobs.count_for_vendor(scope, vendor_id)
            job, created = await uow.analysis_jobs.create_or_get_active(
                scope,
                AnalysisJob(
                    owner_id=scope.owner_id,
                    job_type=AnalysisJobType.VENDOR_ASSESSMENT,
                    vendor_id=vendor_id,
                    params={
                        "depends_on": [str(j) for j in depends_on],
                        "document_ids": [str(d.id) for d in documents],
                    },
                    idempotency_key=f"vendor_assessment:{vendor_id}:{generation}",
                    scoring_config_version=self._config.scoring.version,
                    ruleset_version=self._ruleset.version,
                ),
            )
            dto = analysis_job_to_dto(job)
            version, params = job.scoring_config_version, dict(job.params)
            await uow.commit()
        if created:
            try:
                await self._dispatcher.enqueue_analysis(AnalysisJobType.VENDOR_ASSESSMENT, dto.id)
            except Exception as exc:
                log.error("vendor.dispatch_failed", job_id=str(dto.id), exc_info=True)
                raise DispatchFailed("The assessment could not be scheduled. Try again.") from exc
        return StartedAssessment(dto, version, _pending(params), created=created)

    # ---- run (worker process) ---------------------------------------------------------------

    async def run(self, job_id: UUID, *, attempt: int = 0) -> dict[str, Any]:
        """Wait for the contract analyses this job depends on, then run the graph. While one is
        still running the job goes back to `queued` and the task asks to be retried later: a
        bounded wait that holds no worker slot (spec 25.5)."""
        if self._runner is None:
            raise RuntimeError("this service was built without a runner")
        claim = await self._jobs.claim(ANALYSIS, job_id)
        if not claim.exists:
            raise PermanentTaskError("The assessment job does not exist.", job_code="JOB_NOT_FOUND")
        if not claim.claimed:
            log.info("vendor.skipped", job_id=str(job_id), reason="not claimable")
            return {"status": "skipped"}
        async with self._uow_factory() as uow:
            job = await uow.analysis_jobs.system_get(job_id)
            if job is None or job.vendor_id is None or job.owner_id is None:
                raise PermanentTaskError("The assessment job is malformed.", job_code="JOB_INVALID")
            owner_id, vendor_id = job.owner_id, job.vendor_id
            waiting = []
            for raw in job.params.get("depends_on", []):
                dependency = await uow.analysis_jobs.system_get(UUID(raw))
                if dependency is not None and dependency.status in _ACTIVE:
                    waiting.append(str(dependency.id))
        if waiting:
            await self._jobs.release(ANALYSIS, job_id)
            log.info("vendor.waiting", job_id=str(job_id), pending=waiting)
            return {"status": "waiting", "pending": waiting}
        async with heartbeat(self._jobs, ANALYSIS, job_id):
            outcome = await self._runner.run(job_id=job_id, owner_id=owner_id, vendor_id=vendor_id)
        await self._jobs.mark_succeeded(ANALYSIS, job_id)  # a no-op: persisting already did it
        return {
            "status": "succeeded",
            "assessment_id": str(outcome.assessment_id),
            "resumed": outcome.resumed,
            "attempt": attempt,
        }

    # ---- read -------------------------------------------------------------------------------

    async def latest(self, principal: Principal, vendor_id: UUID) -> AssessmentDTO:
        scope = principal.scope()
        async with self._uow_factory() as uow:
            if await uow.vendors.get(scope, vendor_id) is None:
                raise NotFound("Vendor not found.")
            assessment = await uow.assessments.latest(scope, vendor_id)
            if assessment is None:
                raise NotFound("This vendor has no completed assessment.")
            citations = await uow.citations.for_dimensions(
                scope, [d.id for d in assessment.dimensions]
            )
            job = await uow.analysis_jobs.system_get(assessment.analysis_job_id)
            notes = [str(n) for n in (job.params.get("notes", []) if job else [])]
            return assessment_to_dto(assessment, list(citations), notes)

    async def history(
        self, principal: Principal, vendor_id: UUID, *, limit: int = 20, offset: int = 0
    ) -> list[AssessmentDTO]:
        scope = principal.scope()
        async with self._uow_factory() as uow:
            if await uow.vendors.get(scope, vendor_id) is None:
                raise NotFound("Vendor not found.")
            rows = await uow.assessments.list(scope, vendor_id, limit=limit, offset=offset)
            return [assessment_to_dto(r, []) for r in rows]


def _pending(params: dict[str, Any]) -> list[str]:
    return [f"contract_analysis:{job_id}" for job_id in params.get("depends_on", [])]
