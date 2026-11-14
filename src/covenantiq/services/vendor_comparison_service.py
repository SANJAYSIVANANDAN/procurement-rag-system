"""Comparing vendors from the outside in (spec 21.8): check the preconditions, queue it, run it."""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from covenantiq.analysis.vendor.config_schema import VendorConfig
from covenantiq.core.enums import AnalysisJobType, DocumentStatus
from covenantiq.core.errors import (
    AssessmentRequired,
    Conflict,
    DispatchFailed,
    IncompatibleAssessments,
    NotFound,
    PermanentTaskError,
    ValidationFailed,
)
from covenantiq.core.logging import get_logger
from covenantiq.db.models import AnalysisJob, VendorComparison
from covenantiq.db.uow import UnitOfWork
from covenantiq.graphs.vendor_comparison.runner import VendorComparisonRunner
from covenantiq.services.dto import JobDTO, Principal, analysis_job_to_dto
from covenantiq.services.job_service import ANALYSIS, JobService
from covenantiq.services.task_dispatcher import TaskDispatcher
from covenantiq.workers.heartbeat import heartbeat

log = get_logger(__name__)

MAX_VENDORS = 5


@dataclass(frozen=True)
class StartedComparison:
    comparison_id: UUID
    job: JobDTO


@dataclass(frozen=True)
class ComparisonDTO:
    id: UUID
    job: JobDTO
    vendor_ids: list[str]
    assessment_ids: list[str]
    result: dict[str, Any]
    narrative: str | None
    created_at: datetime


class VendorComparisonService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        dispatcher: TaskDispatcher,
        jobs: JobService,
        config: VendorConfig,
        runner: VendorComparisonRunner | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._dispatcher = dispatcher
        self._jobs = jobs
        self._config = config
        self._runner = runner

    # ---- start (API process) ----------------------------------------------------------------

    async def compare(
        self, principal: Principal, vendor_ids: Sequence[UUID], *, require_fresh: bool = True
    ) -> StartedComparison:
        """The preconditions of spec 21.8, each a 409 with the vendors it concerns."""
        ids = list(dict.fromkeys(vendor_ids))
        if len(ids) != len(vendor_ids):
            raise ValidationFailed("A vendor can appear only once in a comparison.")
        if not 2 <= len(ids) <= MAX_VENDORS:
            raise ValidationFailed(f"Compare between 2 and {MAX_VENDORS} vendors.")
        scope = principal.scope()
        async with self._uow_factory() as uow:
            names: dict[UUID, str] = {}
            assessments = {}
            for vendor_id in ids:
                vendor = await uow.vendors.get(scope, vendor_id)
                if vendor is None:
                    raise NotFound("Vendor not found.")
                names[vendor_id] = vendor.name
                assessments[vendor_id] = await uow.assessments.latest(scope, vendor_id)

            missing = [v for v in ids if assessments[v] is None]
            if missing:
                raise AssessmentRequired(
                    f"Vendor '{names[missing[0]]}' has no completed assessment.",
                    details={"vendor_ids": [str(v) for v in missing]},
                )
            versions = {a.scoring_config_version for a in assessments.values() if a}
            current = self._config.scoring.version
            if len(versions) > 1 or versions != {current}:
                raise IncompatibleAssessments(
                    "The assessments were scored with different configurations. Re-run them.",
                    details={"versions": sorted(versions), "current": current},
                )
            if require_fresh:
                stale = [v for v in ids if await self._is_stale(uow, scope, v, assessments[v])]
                if stale:
                    raise Conflict(
                        f"The assessment of '{names[stale[0]]}' is older than its documents or "
                        "contract analyses. Re-assess it, or compare with require_fresh=false.",
                        details={"vendor_ids": [str(v) for v in stale]},
                    )

            assessment_ids = [str(assessments[v].id) for v in ids]  # type: ignore[union-attr]
            job, _ = await uow.analysis_jobs.create_or_get_active(
                scope,
                AnalysisJob(
                    owner_id=scope.owner_id,
                    job_type=AnalysisJobType.VENDOR_COMPARISON,
                    params={"vendor_ids": [str(v) for v in ids], "assessment_ids": assessment_ids},
                    idempotency_key=f"vendor_comparison:{uuid.uuid4().hex}",
                    scoring_config_version=current,
                ),
            )
            comparison = await uow.comparisons.add(
                scope,
                VendorComparison(
                    owner_id=scope.owner_id,
                    analysis_job_id=job.id,
                    vendor_ids=[str(v) for v in ids],
                    assessment_ids=assessment_ids,
                    result={},
                ),
            )
            dto, comparison_id = analysis_job_to_dto(job), comparison.id
            await uow.commit()
        try:
            await self._dispatcher.enqueue_analysis(AnalysisJobType.VENDOR_COMPARISON, dto.id)
        except Exception as exc:
            log.error("comparison.dispatch_failed", job_id=str(dto.id), exc_info=True)
            raise DispatchFailed("The comparison could not be scheduled. Try again.") from exc
        return StartedComparison(comparison_id, dto)

    @staticmethod
    async def _is_stale(uow: UnitOfWork, scope: Any, vendor_id: UUID, assessment: Any) -> bool:
        documents = await uow.documents.list(
            scope, status=DocumentStatus.READY, vendor_id=vendor_id, limit=200
        )
        newest_document = max((d.updated_at for d in documents), default=None)
        newest_analysis = await uow.analysis_jobs.latest_finished_at(
            scope, [d.id for d in documents], AnalysisJobType.CONTRACT_ANALYSIS
        )
        newest = max((t for t in (newest_document, newest_analysis) if t is not None), default=None)
        return newest is not None and assessment.assessed_at < newest

    # ---- run (worker process) ---------------------------------------------------------------

    async def run(self, job_id: UUID, *, attempt: int = 0) -> dict[str, Any]:
        if self._runner is None:
            raise RuntimeError("this service was built without a runner")
        claim = await self._jobs.claim(ANALYSIS, job_id)
        if not claim.exists:
            raise PermanentTaskError("The comparison job does not exist.", job_code="JOB_NOT_FOUND")
        if not claim.claimed:
            return {"status": "skipped"}
        async with self._uow_factory() as uow:
            job = await uow.analysis_jobs.system_get(job_id)
            comparison = await uow.comparisons.system_for_job(job_id)
            if job is None or comparison is None or job.owner_id is None:
                raise PermanentTaskError("The comparison job is malformed.", job_code="JOB_INVALID")
            owner_id, comparison_id = job.owner_id, comparison.id
            vendor_ids = [UUID(str(v)) for v in comparison.vendor_ids]
        async with heartbeat(self._jobs, ANALYSIS, job_id):
            outcome = await self._runner.run(
                job_id=job_id, owner_id=owner_id, comparison_id=comparison_id, vendor_ids=vendor_ids
            )
        await self._jobs.mark_succeeded(ANALYSIS, job_id)  # a no-op: persisting already did it
        return {"status": "succeeded", "narrative": outcome.narrative_source, "attempt": attempt}

    # ---- read -------------------------------------------------------------------------------

    async def get(self, principal: Principal, comparison_id: UUID) -> ComparisonDTO:
        scope = principal.scope()
        async with self._uow_factory() as uow:
            comparison = await uow.comparisons.get(scope, comparison_id)
            if comparison is None:
                raise NotFound("Comparison not found.")
            job = await uow.analysis_jobs.get(scope, comparison.analysis_job_id)
            if job is None:
                raise NotFound("Comparison not found.")
            return ComparisonDTO(
                id=comparison.id,
                job=analysis_job_to_dto(job),
                vendor_ids=[str(v) for v in comparison.vendor_ids],
                assessment_ids=[str(a) for a in comparison.assessment_ids],
                result=dict(comparison.result),
                narrative=comparison.narrative,
                created_at=comparison.created_at,
            )
