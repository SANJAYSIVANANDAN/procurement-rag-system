"""`AnalysisStorePort` and `ProgressPort` on PostgreSQL: each call is its own unit of work."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from covenantiq.analysis.contract.types import FindingDraft
from covenantiq.core.enums import JobStatus
from covenantiq.core.errors import NotFound
from covenantiq.db.models import Citation, ContractFinding
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.graphs.contract_analysis.deps import ChunkText, DocumentInfo
from covenantiq.rag.citations import CitationValidator
from covenantiq.services.job_service import ANALYSIS, JobService


class PostgresAnalysisStore:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        jobs: JobService,
        citations: CitationValidator | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._jobs = jobs
        self._citations = citations or CitationValidator()

    async def load_document(self, owner_id: UUID, document_id: UUID) -> DocumentInfo | None:
        scope = OwnershipScope(owner_id)
        async with self._uow_factory() as uow:
            document = await uow.documents.get(scope, document_id)
            if document is None:
                return None
            quality: dict[str, Any] = dict(document.extraction_quality)
            pages = quality.get("pages_total")
            types = await uow.clauses.clause_types_present(scope, document_id)
            return DocumentInfo(
                id=document.id,
                title=document.title,
                status=document.status.value,
                contract_type=document.contract_type.value,
                extraction_quality=quality,
                pages_total=pages if isinstance(pages, int) else None,
                clause_types_present=sorted(types),
                updated_at=document.updated_at,
            )

    async def document_chunks(self, owner_id: UUID, document_id: UUID) -> list[ChunkText]:
        scope = OwnershipScope(owner_id)
        async with self._uow_factory() as uow:
            document = await uow.documents.get(scope, document_id)
            if document is None:
                return []
            rows = await uow.chunks.list_for_document(scope, document_id)
            return [
                ChunkText(
                    chunk_id=chunk.id,
                    clause_id=chunk.clause_id,
                    document_id=document_id,
                    document_title=document.title,
                    text=chunk.text,
                    clause_ref=clause_ref,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                )
                for chunk, clause_ref in rows
            ]

    async def persist(
        self,
        owner_id: UUID,
        job_id: UUID,
        document_id: UUID,
        findings: Sequence[FindingDraft],
        stats: dict[str, Any],
    ) -> None:
        """Delete the job's findings, insert the new ones with their citations, and mark the job
        succeeded: one transaction (spec 20.8). Safe to run again after a crash mid-way."""
        scope = OwnershipScope(owner_id)
        async with self._uow_factory() as uow:
            job = await uow.analysis_jobs.system_get(job_id)
            if job is None or job.owner_id != owner_id:
                raise NotFound("Analysis job not found.")
            await uow.findings.delete_for_job(scope, job_id)
            rows = [
                ContractFinding(
                    owner_id=owner_id,
                    analysis_job_id=job_id,
                    document_id=document_id,
                    rule_id=d.rule_id,
                    ordinal=d.ordinal,
                    risk_category=d.risk_category,
                    clause_status=d.clause_status,
                    severity=d.severity,
                    title=d.title,
                    explanation=d.explanation,
                    business_impact=d.business_impact,
                    recommended_action=d.recommended_action,
                    clause_ref=d.clause_ref,
                    page_number=d.page_number,
                    confidence=d.confidence,
                    evidence_sufficiency=d.evidence_sufficiency,
                    details=d.details,
                )
                for d in findings
            ]
            await uow.findings.bulk_add(scope, rows)
            citations = [
                Citation(
                    finding_id=row.id,
                    document_id=source.document_id,
                    chunk_id=source.chunk_ids[0] if source.chunk_ids else None,
                    clause_id=source.clause_id,
                    marker=position,
                    quote=self._citations.quote_for(source.text, [draft.explanation]),
                    document_title=source.document_title,
                    page_start=source.page_start,
                    page_end=source.page_end,
                    clause_ref=source.clause_ref,
                    relevance_score=source.rerank_score,
                    validated=True,
                )
                for row, draft in zip(rows, findings, strict=True)
                for position, source in enumerate(draft.evidence, start=1)
            ]
            await uow.citations.bulk_add(scope, citations)
            await uow.analysis_jobs.system_transition(
                job_id,
                [JobStatus.RUNNING],
                JobStatus.SUCCEEDED,
                progress=1.0,
                finished_at=datetime.now(UTC),
                params={**job.params, "stats": stats},
            )
            await uow.commit()

    async def report(self, job_id: UUID, stage: str, fraction: float) -> None:
        await self._jobs.touch(ANALYSIS, job_id, stage=stage, progress=fraction)
