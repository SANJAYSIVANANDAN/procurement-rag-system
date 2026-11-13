from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select

from covenantiq.db.models import Vendor
from covenantiq.db.repositories.base import Repository, ensure_owned, like_pattern
from covenantiq.db.scope import OwnershipScope


class VendorRepository(Repository):
    async def get(self, scope: OwnershipScope, vendor_id: UUID) -> Vendor | None:
        statement = scope.apply(select(Vendor).where(Vendor.id == vendor_id), Vendor)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_by_normalized_name(
        self, scope: OwnershipScope, normalized_name: str
    ) -> Vendor | None:
        statement = scope.apply(
            select(Vendor).where(Vendor.normalized_name == normalized_name), Vendor
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def list(
        self, scope: OwnershipScope, *, q: str | None = None, limit: int = 50, offset: int = 0
    ) -> Sequence[Vendor]:
        statement = scope.apply(select(Vendor), Vendor)
        if q:
            statement = statement.where(Vendor.normalized_name.ilike(like_pattern(q), escape="\\"))
        statement = (
            statement.order_by(Vendor.normalized_name, Vendor.id).limit(limit).offset(offset)
        )
        return (await self.session.execute(statement)).scalars().all()

    async def add(self, scope: OwnershipScope, vendor: Vendor) -> Vendor:
        ensure_owned(scope, vendor)
        self.session.add(vendor)
        await self.session.flush()
        return vendor

    async def update(self, scope: OwnershipScope, vendor_id: UUID, **fields: Any) -> Vendor | None:
        vendor = await self.get(scope, vendor_id)
        if vendor is None:
            return None
        for name, value in fields.items():
            setattr(vendor, name, value)
        await self.session.flush()
        return vendor
