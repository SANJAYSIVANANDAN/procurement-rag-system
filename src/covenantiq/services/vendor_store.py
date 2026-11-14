"""The vendor graphs' ports on PostgreSQL: each call is its own unit of work."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from covenantiq.analysis.contract.status import ExtractionQualityInfo
from covenantiq.analysis.vendor.comparison import (
    AssessmentSnapshot,
    DimensionSnapshot,
    SignalSnapshot,
)
from covenantiq.analysis.vendor.types import EvidenceRef
from covenantiq.core.enums import AnalysisJobType, DocumentStatus, JobStatus, RiskDimension
from covenantiq.core.errors import NotFound
from covenantiq.db.models import Citation, VendorRiskAssessment, VendorRiskDimension
from covenantiq.db.repositories.analysis_jobs import ACTIVE_STATUSES
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.graphs.vendor_assessment.deps import (
    AssessmentRecord,
    DocumentAnalysis,
    FindingFact,
    VendorDocument,
    VendorScope,
)
from covenantiq.graphs.vendor_comparison.deps import LoadedAssessment
from covenantiq.rag.config import AnalysisConfig
from covenantiq.services.job_service import ANALYSIS, JobService

DOCUMENT_LIMIT = 200
QUOTES_PER_DIMENSION = 3


def evidence_of(citation: Citation) -> EvidenceRef:
    return EvidenceRef(
        document_id=citation.document_id,
        document_title=citation.document_title,
        quote=citation.quote,
        page_start=citation.page_start,
        page_end=citation.page_end,
        clause_ref=citation.clause_ref,
        chunk_id=citation.chunk_id,
        clause_id=citation.clause_id,
    )


class PostgresVendorStore:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        jobs: JobService,
        analysis: AnalysisConfig | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._jobs = jobs
        self._analysis = analysis or AnalysisConfig()

    # ---- assessment -------------------------------------------------------------------------

    async def load_scope(self, owner_id: UUID, vendor_id: UUID) -> VendorScope | None:
        scope = OwnershipScope(owner_id)
        async with self._uow_factory() as uow:
            vendor = await uow.vendors.get(scope, vendor_id)
            if vendor is None:
                return None
            documents = await uow.documents.list(
                scope, status=DocumentStatus.READY, vendor_id=vendor_id, limit=DOCUMENT_LIMIT
            )
            return VendorScope(
                vendor_id=vendor.id,
                name=vendor.name,
                documents=[
                    VendorDocument(
                        id=d.id,
                        title=d.title,
                        extraction_ok=ExtractionQualityInfo.from_document(
                            d.extraction_quality, self._analysis
                        ).ok,
                        updated_at=d.updated_at,
                    )
                    for d in sorted(documents, key=lambda d: str(d.id))
                ],
            )

    async def document_analyses(
        self, owner_id: UUID, document_ids: Sequence[UUID], ruleset_version: str
    ) -> list[DocumentAnalysis]:
        """Where each document's contract analysis stands: a current one (with its findings), one
        running, one that failed, or none."""
        scope = OwnershipScope(owner_id)
        analyses: list[DocumentAnalysis] = []
        async with self._uow_factory() as uow:
            for document_id in document_ids:
                document = await uow.documents.get(scope, document_id)
                if document is None:
                    continue
                succeeded = await uow.findings.latest_succeeded_job(scope, document_id)
                if (
                    succeeded is not None
                    and succeeded.ruleset_version == ruleset_version
                    and succeeded.finished_at is not None
                    and document.updated_at <= succeeded.finished_at
                ):
                    rows = list(await uow.findings.for_job(scope, succeeded.id))
                    cites = await uow.citations.for_findings(scope, [r.id for r in rows])
                    by_finding: dict[UUID, list[EvidenceRef]] = {}
                    for citation in cites:
                        if citation.finding_id is not None:
                            by_finding.setdefault(citation.finding_id, []).append(
                                evidence_of(citation)
                            )
                    analyses.append(
                        DocumentAnalysis(
                            document_id=document_id,
                            state="current",
                            job_id=succeeded.id,
                            findings=[
                                FindingFact(
                                    rule_id=r.rule_id,
                                    status=r.clause_status,
                                    severity=r.severity,
                                    evidence=by_finding.get(r.id, []),
                                )
                                for r in rows
                            ],
                        )
                    )
                    continue
                latest = await uow.analysis_jobs.latest_for_document(
                    scope, document_id, AnalysisJobType.CONTRACT_ANALYSIS
                )
                if latest is None or latest.ruleset_version != ruleset_version:
                    analyses.append(DocumentAnalysis(document_id=document_id, state="missing"))
                elif latest.status in ACTIVE_STATUSES:
                    analyses.append(
                        DocumentAnalysis(document_id=document_id, state="pending", job_id=latest.id)
                    )
                elif latest.status is JobStatus.FAILED and latest.created_at >= document.updated_at:
                    analyses.append(
                        DocumentAnalysis(document_id=document_id, state="failed", job_id=latest.id)
                    )
                else:
                    analyses.append(DocumentAnalysis(document_id=document_id, state="missing"))
        return analyses

    async def persist_assessment(
        self, owner_id: UUID, job_id: UUID, record: AssessmentRecord
    ) -> UUID:
        """The assessment, its dimensions, their citations, the `is_latest` flip and the job's
        success: one transaction. Storing it again for the same job replaces it."""
        scope = OwnershipScope(owner_id)
        async with self._uow_factory() as uow:
            job = await uow.analysis_jobs.system_get(job_id)
            if job is None or job.owner_id != owner_id:
                raise NotFound("Assessment job not found.")
            await uow.assessments.delete_for_job(scope, job_id)
            score = record.score
            dimensions = [
                VendorRiskDimension(
                    dimension=d.dimension,
                    score=d.score,
                    severity=d.severity,
                    coverage=d.coverage,
                    status=d.status,
                    reasoning=record.reasoning.get(d.dimension.value),
                    signals=record.dimension_signals.get(d.dimension.value, []),
                    missing_information=[m.model_dump(mode="json") for m in d.missing_information],
                    mitigations=record.mitigations.get(d.dimension.value, []),
                )
                for d in score.dimensions
            ]
            assessment = await uow.assessments.add_assessment(
                scope,
                VendorRiskAssessment(
                    owner_id=owner_id,
                    vendor_id=record.vendor_id,
                    analysis_job_id=job_id,
                    overall_score=score.overall_score,
                    overall_severity=score.overall_severity,
                    evidence_coverage=score.evidence_coverage,
                    completeness=score.completeness,
                    provisional=score.provisional,
                    weights=record.weights,
                    scoring_config_version=record.scoring_config_version,
                    documents_considered=record.documents,
                ),
                dimensions,
            )
            citations = [
                Citation(
                    risk_dimension_id=dimension.id,
                    document_id=item.document_id,
                    chunk_id=item.chunk_id,
                    clause_id=item.clause_id,
                    marker=position,
                    quote=item.quote,
                    document_title=item.document_title,
                    page_start=item.page_start,
                    page_end=item.page_end,
                    clause_ref=item.clause_ref,
                    validated=True,
                )
                for dimension in dimensions
                for position, item in enumerate(
                    record.evidence.get(dimension.dimension.value, []), 1
                )
            ]
            await uow.citations.bulk_add(scope, citations)
            await uow.analysis_jobs.system_transition(
                job_id,
                [JobStatus.RUNNING],
                JobStatus.SUCCEEDED,
                progress=1.0,
                finished_at=datetime.now(UTC),
                params={**job.params, "notes": record.notes},
            )
            assessment_id = assessment.id
            await uow.commit()
        return assessment_id

    # ---- comparison -------------------------------------------------------------------------

    async def load_assessments(
        self, owner_id: UUID, vendor_ids: Sequence[UUID]
    ) -> list[LoadedAssessment]:
        scope = OwnershipScope(owner_id)
        loaded: list[LoadedAssessment] = []
        async with self._uow_factory() as uow:
            for vendor_id in vendor_ids:
                assessment = await uow.assessments.latest(scope, vendor_id)
                vendor = await uow.vendors.get(scope, vendor_id)
                if assessment is None or vendor is None:
                    continue
                by_id = {d.id: d for d in assessment.dimensions}
                cites = await uow.citations.for_dimensions(scope, list(by_id))
                quotes: dict[str, list[str]] = {}
                for citation in cites:
                    if citation.risk_dimension_id is None:
                        continue
                    name = by_id[citation.risk_dimension_id].dimension.value
                    if len(quotes.setdefault(name, [])) < QUOTES_PER_DIMENSION:
                        quotes[name].append(citation.quote)
                loaded.append(
                    LoadedAssessment(
                        snapshot=_snapshot(vendor.id, vendor.name, assessment),
                        scoring_config_version=assessment.scoring_config_version,
                        quotes=quotes,
                    )
                )
        return loaded

    async def persist_comparison(
        self,
        owner_id: UUID,
        job_id: UUID,
        comparison_id: UUID,
        result: dict[str, Any],
        narrative: str,
    ) -> None:
        scope = OwnershipScope(owner_id)
        async with self._uow_factory() as uow:
            job = await uow.analysis_jobs.system_get(job_id)
            if job is None or job.owner_id != owner_id:
                raise NotFound("Comparison job not found.")
            if not await uow.comparisons.set_result(scope, comparison_id, result, narrative):
                raise NotFound("Comparison not found.")
            await uow.analysis_jobs.system_transition(
                job_id,
                [JobStatus.RUNNING],
                JobStatus.SUCCEEDED,
                progress=1.0,
                finished_at=datetime.now(UTC),
            )
            await uow.commit()

    # ---- progress ---------------------------------------------------------------------------

    async def report(self, job_id: UUID, stage: str, fraction: float) -> None:
        await self._jobs.touch(ANALYSIS, job_id, stage=stage, progress=fraction)


def _signal_status(row: dict[str, Any]) -> str:
    """A stored signal as the comparison reads it: what the documents said, before the absent
    policy turned an absence into a risk value."""
    if row.get("policy_treated") or row.get("gap") == "absent_in_all_documents":
        return "absent"
    if row.get("gap") == "insufficient_evidence":
        return "insufficient_evidence"
    return str(row["status"])


def _snapshot(vendor_id: UUID, name: str, assessment: VendorRiskAssessment) -> AssessmentSnapshot:
    signals = [
        SignalSnapshot(
            signal_id=row["signal_id"],
            dimension=dimension.dimension,
            label=row.get("label", row["signal_id"]),
            status=_signal_status(row),
            policy_treated=bool(row.get("policy_treated")),
        )
        for dimension in assessment.dimensions
        for row in dimension.signals
    ]
    return AssessmentSnapshot(
        vendor_id=vendor_id,
        vendor_name=name,
        assessment_id=assessment.id,
        overall_score=assessment.overall_score,
        provisional=assessment.provisional,
        dimensions=[
            DimensionSnapshot(
                dimension=RiskDimension(d.dimension),
                status=d.status,
                score=d.score,
                severity=d.severity,
                coverage=d.coverage,
            )
            for d in assessment.dimensions
        ],
        signals=signals,
    )
