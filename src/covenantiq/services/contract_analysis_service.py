"""Contract analysis from the outside in (spec 20.1): start it, run it, read its findings."""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from covenantiq.analysis.contract.findings import DISCLAIMER
from covenantiq.analysis.contract.rules_loader import Ruleset
from covenantiq.core.enums import (
    AnalysisJobType,
    ClauseStatus,
    DocumentStatus,
    EvidenceSufficiency,
    JobStatus,
    RiskCategory,
    Severity,
)
from covenantiq.core.errors import (
    DispatchFailed,
    DocumentNotReady,
    NotFound,
    PermanentTaskError,
    ValidationFailed,
)
from covenantiq.core.logging import get_logger
from covenantiq.db.models import AnalysisJob, Citation, ContractFinding
from covenantiq.db.uow import UnitOfWork
from covenantiq.graphs.contract_analysis.runner import ContractAnalysisRunner
from covenantiq.services.dto import JobDTO, Principal, analysis_job_to_dto
from covenantiq.services.job_service import ANALYSIS, JobService
from covenantiq.services.task_dispatcher import TaskDispatcher
from covenantiq.workers.heartbeat import heartbeat

log = get_logger(__name__)

SEVERITY_RANK = {s: i for i, s in enumerate(reversed(list(Severity)))}  # critical first


@dataclass(frozen=True)
class StartedAnalysis:
    job: JobDTO
    ruleset_version: str | None
    created: bool  # False: an analysis that is running, or still current, was returned


@dataclass(frozen=True)
class EvidenceDTO:
    marker: int | None
    quote: str
    document_id: UUID
    document_title: str
    page_start: int | None
    page_end: int | None
    clause_ref: str | None
    validated: bool


@dataclass(frozen=True)
class FindingDTO:
    id: UUID
    rule_id: str
    risk_category: RiskCategory
    clause_status: ClauseStatus
    severity: Severity
    title: str
    explanation: str
    business_impact: str | None
    recommended_action: str | None
    clause_ref: str | None
    page_number: int | None
    confidence: float
    evidence_sufficiency: EvidenceSufficiency
    evidence: list[EvidenceDTO]
    analyzed_at: datetime
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FindingsReportDTO:
    document_id: UUID
    job_id: UUID
    ruleset_version: str | None
    analyzed_at: datetime | None
    summary: dict[str, int]
    findings: list[FindingDTO]
    notes: list[str]
    disclaimer: str = DISCLAIMER


def finding_to_dto(finding: ContractFinding, evidence: Sequence[Citation]) -> FindingDTO:
    return FindingDTO(
        id=finding.id,
        rule_id=finding.rule_id,
        risk_category=finding.risk_category,
        clause_status=finding.clause_status,
        severity=finding.severity,
        title=finding.title,
        explanation=finding.explanation,
        business_impact=finding.business_impact,
        recommended_action=finding.recommended_action,
        clause_ref=finding.clause_ref,
        page_number=finding.page_number,
        confidence=finding.confidence or 0.0,
        evidence_sufficiency=finding.evidence_sufficiency,
        evidence=[
            EvidenceDTO(
                marker=c.marker,
                quote=c.quote,
                document_id=c.document_id,
                document_title=c.document_title,
                page_start=c.page_start,
                clause_ref=c.clause_ref,
                page_end=c.page_end,
                validated=c.validated,
            )
            for c in evidence
        ],
        analyzed_at=finding.analyzed_at,
        details=finding.details,
    )


def summarise(findings: Sequence[FindingDTO]) -> dict[str, int]:
    """Counts by severity, plus the two statuses a reader must not miss (spec 16)."""
    by_severity = Counter(f.severity.value for f in findings)
    by_status = Counter(f.clause_status.value for f in findings)
    summary = {s.value: by_severity.get(s.value, 0) for s in reversed(list(Severity))}
    summary["absent"] = by_status.get(ClauseStatus.ABSENT.value, 0)
    summary["insufficient_evidence"] = by_status.get(ClauseStatus.INSUFFICIENT_EVIDENCE.value, 0)
    return summary


class ContractAnalysisService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        dispatcher: TaskDispatcher,
        jobs: JobService,
        ruleset: Ruleset,
        runner: ContractAnalysisRunner | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._dispatcher = dispatcher
        self._jobs = jobs
        self._ruleset = ruleset
        self._runner = runner

    # ---- start (API process) ----------------------------------------------------------------

    async def start(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        ruleset: str | None = None,
        force: bool = False,
    ) -> StartedAnalysis:
        """Verify ownership and readiness, then create the job (or return the running or current
        one) and queue it (spec 20.1). The job row is committed before the message is sent, so a
        failed send leaves a `queued` job that the reconciliation sweep re-publishes."""
        scope = principal.scope()
        if ruleset is not None and ruleset != self._ruleset.file.ruleset:
            raise ValidationFailed(f"Unknown ruleset '{ruleset}'.")
        async with self._uow_factory() as uow:
            document = await uow.documents.get(scope, document_id)
            if document is None:
                raise NotFound("Document not found.")
            if document.status is not DocumentStatus.READY:
                raise DocumentNotReady("The document must finish processing before it is analysed.")
            running = await uow.analysis_jobs.active_for_document(
                scope, document_id, AnalysisJobType.CONTRACT_ANALYSIS
            )
            if running is not None:  # never two at once for one document, forced or not
                return StartedAnalysis(
                    analysis_job_to_dto(running), running.ruleset_version, created=False
                )
            if not force:
                current = await uow.findings.latest_succeeded_job(scope, document_id)
                if (
                    current is not None
                    and current.ruleset_version == self._ruleset.version
                    and current.finished_at is not None
                    and document.updated_at <= current.finished_at
                ):
                    return StartedAnalysis(
                        analysis_job_to_dto(current), current.ruleset_version, created=False
                    )
            generation = await uow.analysis_jobs.count_for_document(scope, document_id)
            job, created = await uow.analysis_jobs.create_or_get_active(
                scope,
                AnalysisJob(
                    owner_id=scope.owner_id,
                    job_type=AnalysisJobType.CONTRACT_ANALYSIS,
                    document_id=document_id,
                    params={"contract_type": document.contract_type.value},
                    idempotency_key=f"contract_analysis:{document_id}:{generation}",
                    ruleset_version=self._ruleset.version,
                ),
            )
            dto = analysis_job_to_dto(job)
            version = job.ruleset_version
            await uow.commit()
        if created:
            try:
                await self._dispatcher.enqueue_analysis(AnalysisJobType.CONTRACT_ANALYSIS, dto.id)
            except Exception as exc:
                log.error("analysis.dispatch_failed", job_id=str(dto.id), exc_info=True)
                raise DispatchFailed("The analysis could not be scheduled. Try again.") from exc
        return StartedAnalysis(dto, version, created=created)

    # ---- run (worker process) ---------------------------------------------------------------

    async def run(self, job_id: UUID, *, attempt: int = 0) -> dict[str, Any]:
        if self._runner is None:
            raise RuntimeError("this service was built without a runner")
        claim = await self._jobs.claim(ANALYSIS, job_id)
        if not claim.exists:
            raise PermanentTaskError("The analysis job does not exist.", job_code="JOB_NOT_FOUND")
        if not claim.claimed:
            log.info("analysis.skipped", job_id=str(job_id), reason="not claimable")
            return {"status": "skipped"}
        async with self._uow_factory() as uow:
            job = await uow.analysis_jobs.system_get(job_id)
            if job is None or job.document_id is None or job.owner_id is None:
                raise PermanentTaskError("The analysis job is malformed.", job_code="JOB_INVALID")
            owner_id, document_id = job.owner_id, job.document_id
        async with heartbeat(self._jobs, ANALYSIS, job_id):
            outcome = await self._runner.run(
                job_id=job_id, owner_id=owner_id, document_id=document_id
            )
        await self._jobs.mark_succeeded(ANALYSIS, job_id)  # a no-op: persisting already did it
        log.info("analysis.done", job_id=str(job_id), attempt=attempt, findings=outcome.findings)
        return {"status": "succeeded", "findings": outcome.findings, "resumed": outcome.resumed}

    # ---- read -------------------------------------------------------------------------------

    async def findings(
        self,
        principal: Principal,
        document_id: UUID,
        *,
        job_id: UUID | None = None,
        severity: Severity | None = None,
        status: ClauseStatus | None = None,
        category: RiskCategory | None = None,
    ) -> FindingsReportDTO:
        """The findings of one analysis (the document's latest succeeded one by default)."""
        scope = principal.scope()
        async with self._uow_factory() as uow:
            if await uow.documents.get(scope, document_id) is None:
                raise NotFound("Document not found.")
            if job_id is None:
                job = await uow.findings.latest_succeeded_job(scope, document_id)
            else:
                job = await uow.analysis_jobs.get(scope, job_id)
                if job is not None and (
                    job.document_id != document_id or job.status is not JobStatus.SUCCEEDED
                ):
                    job = None
            if job is None:
                raise NotFound("No completed analysis was found for this document.")
            rows = list(await uow.findings.for_job(scope, job.id))
            citations = await uow.citations.for_findings(scope, [r.id for r in rows])
            by_finding: dict[UUID, list[Citation]] = {}
            for citation in citations:
                if citation.finding_id is not None:
                    by_finding.setdefault(citation.finding_id, []).append(citation)
            dtos = [finding_to_dto(r, by_finding.get(r.id, [])) for r in rows]
            stats: dict[str, Any] = dict(job.params.get("stats", {}))
            analyzed = max((r.analyzed_at for r in rows), default=job.finished_at)
            result_job, version = job.id, job.ruleset_version
        notes: list[str] = list(stats.get("notes", []))
        summary = summarise(dtos)  # the whole analysis, whatever the filters
        wanted = [
            f
            for f in dtos
            if (severity is None or f.severity is severity)
            and (status is None or f.clause_status is status)
            and (category is None or f.risk_category is category)
        ]
        wanted.sort(key=lambda f: (SEVERITY_RANK[f.severity], f.rule_id))
        return FindingsReportDTO(
            document_id=document_id,
            job_id=result_job,
            ruleset_version=version,
            analyzed_at=analyzed,
            summary=summary,
            findings=wanted,
            notes=notes,
        )
