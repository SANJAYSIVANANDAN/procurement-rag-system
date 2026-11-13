from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select

from covenantiq.core.enums import JobStatus
from covenantiq.db.models import AnalysisJob, ContractFinding
from covenantiq.db.repositories.base import Repository, ensure_owned
from covenantiq.db.scope import OwnershipScope


class FindingRepository(Repository):
    async def bulk_add(self, scope: OwnershipScope, findings: Sequence[ContractFinding]) -> None:
        for finding in findings:
            ensure_owned(scope, finding)
        self.session.add_all(findings)
        await self.session.flush()

    async def for_job(self, scope: OwnershipScope, job_id: UUID) -> Sequence[ContractFinding]:
        statement = scope.apply(
            select(ContractFinding)
            .where(ContractFinding.analysis_job_id == job_id)
            .order_by(ContractFinding.rule_id, ContractFinding.ordinal),
            ContractFinding,
        )
        return (await self.session.execute(statement)).scalars().all()

    async def latest_for_document(
        self, scope: OwnershipScope, document_id: UUID
    ) -> Sequence[ContractFinding]:
        """Findings of the document's most recent succeeded analysis; empty if there is none."""
        latest_job = scope.apply(
            select(AnalysisJob.id)
            .where(
                AnalysisJob.document_id == document_id,
                AnalysisJob.status == JobStatus.SUCCEEDED,
            )
            .order_by(AnalysisJob.created_at.desc(), AnalysisJob.id.desc())
            .limit(1),
            AnalysisJob,
        )
        job_id = (await self.session.execute(latest_job)).scalar_one_or_none()
        if job_id is None:
            return []
        return await self.for_job(scope, job_id)
