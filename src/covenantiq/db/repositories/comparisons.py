from uuid import UUID

from sqlalchemy import select

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
