from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import selectinload

from covenantiq.core.ids import uuid7
from covenantiq.db.models import VendorRiskAssessment, VendorRiskDimension
from covenantiq.db.repositories.base import Repository, ensure_owned
from covenantiq.db.scope import OwnershipScope


class VendorAssessmentRepository(Repository):
    async def add_assessment(
        self,
        scope: OwnershipScope,
        assessment: VendorRiskAssessment,
        dimensions: Sequence[VendorRiskDimension],
    ) -> VendorRiskAssessment:
        """Store an assessment as the vendor's latest, demoting the previous latest in the same
        transaction so the partial unique index never sees two."""
        ensure_owned(scope, assessment)
        await self.session.execute(
            scope.apply(
                update(VendorRiskAssessment)
                .where(
                    VendorRiskAssessment.vendor_id == assessment.vendor_id,
                    VendorRiskAssessment.is_latest.is_(True),
                )
                .values(is_latest=False)
                .execution_options(synchronize_session=False),
                VendorRiskAssessment,
            )
        )
        assessment.id = assessment.id or uuid7()
        assessment.is_latest = True
        assessment.dimensions = list(dimensions)
        self.session.add(assessment)
        await self.session.flush()
        return assessment

    async def latest(self, scope: OwnershipScope, vendor_id: UUID) -> VendorRiskAssessment | None:
        statement = scope.apply(
            select(VendorRiskAssessment)
            .where(
                VendorRiskAssessment.vendor_id == vendor_id,
                VendorRiskAssessment.is_latest.is_(True),
            )
            .options(selectinload(VendorRiskAssessment.dimensions)),
            VendorRiskAssessment,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def list(
        self, scope: OwnershipScope, vendor_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> Sequence[VendorRiskAssessment]:
        statement = scope.apply(
            select(VendorRiskAssessment)
            .where(VendorRiskAssessment.vendor_id == vendor_id)
            .options(selectinload(VendorRiskAssessment.dimensions))
            .order_by(VendorRiskAssessment.assessed_at.desc(), VendorRiskAssessment.id.desc())
            .limit(limit)
            .offset(offset),
            VendorRiskAssessment,
        )
        return (await self.session.execute(statement)).scalars().all()

    async def delete_for_job(self, scope: OwnershipScope, job_id: UUID) -> int:
        """Drop the assessment a job stored (its dimensions and citations go with it), so that
        storing it again after a crash leaves one."""
        result = await self.session.execute(
            scope.apply(
                delete(VendorRiskAssessment).where(VendorRiskAssessment.analysis_job_id == job_id),
                VendorRiskAssessment,
            )
        )
        return int(result.rowcount)  # type: ignore[attr-defined]
