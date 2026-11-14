from typing import Any
from uuid import UUID

from sqlalchemy import select, update

from covenantiq.db.models import VendorComparison
from covenantiq.db.repositories.base import Repository, ensure_owned
from covenantiq.db.scope import OwnershipScope


class ComparisonRepository(Repository):
    async def add(self, scope: OwnershipScope, comparison: VendorComparison) -> VendorComparison:
        ensure_owned(scope, comparison)
        self.session.add(comparison)
        await self.session.flush()
        return comparison

    async def get(self, scope: OwnershipScope, comparison_id: UUID) -> VendorComparison | None:
        statement = scope.apply(
            select(VendorComparison).where(VendorComparison.id == comparison_id),
            VendorComparison,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def set_result(
        self,
        scope: OwnershipScope,
        comparison_id: UUID,
        result: dict[str, Any],
        narrative: str | None,
    ) -> bool:
        statement = scope.apply(
            update(VendorComparison)
            .where(VendorComparison.id == comparison_id)
            .values(result=result, narrative=narrative)
            .execution_options(synchronize_session=False),
            VendorComparison,
        )
        return int((await self.session.execute(statement)).rowcount) > 0  # type: ignore[attr-defined]

    async def system_for_job(self, job_id: UUID) -> VendorComparison | None:
        statement = select(VendorComparison).where(VendorComparison.analysis_job_id == job_id)
        return (await self.session.execute(statement)).scalar_one_or_none()
