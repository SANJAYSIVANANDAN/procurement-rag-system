import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from covenantiq.core.errors import Conflict, NotFound, ValidationFailed
from covenantiq.db.models import Vendor
from covenantiq.db.uow import UnitOfWork
from covenantiq.services.dto import (
    Page,
    Principal,
    VendorDetailDTO,
    VendorDTO,
    document_to_dto,
    vendor_to_dto,
)
from covenantiq.services.pagination import decode_cursor, encode_cursor

MAX_NAME_LENGTH = 255
PATCHABLE_FIELDS = frozenset({"name", "industry", "website", "metadata"})


def clean_vendor_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ValidationFailed("The vendor name must not be empty.")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ValidationFailed(f"The vendor name must be at most {MAX_NAME_LENGTH} characters.")
    return cleaned


def normalize_vendor_name(name: str) -> str:
    """The key vendors are unique by: trimmed, whitespace collapsed, lower-cased."""
    return re.sub(r"\s+", " ", name.strip()).lower()


class VendorService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def create(
        self,
        principal: Principal,
        *,
        name: str,
        industry: str | None = None,
        website: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> VendorDTO:
        scope = principal.scope()
        name = clean_vendor_name(name)
        normalized = normalize_vendor_name(name)
        async with self._uow_factory() as uow:
            if await uow.vendors.get_by_normalized_name(scope, normalized) is not None:
                raise Conflict("A vendor with this name already exists.")
            try:
                vendor = await uow.vendors.add(
                    scope,
                    Vendor(
                        owner_id=scope.owner_id,
                        name=name,
                        normalized_name=normalized,
                        industry=industry,
                        website=website,
                        metadata_=metadata or {},
                    ),
                )
            except IntegrityError:
                raise Conflict("A vendor with this name already exists.") from None
            await uow.commit()
            return vendor_to_dto(vendor)

    async def list(
        self,
        principal: Principal,
        *,
        q: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[VendorDTO]:
        after: tuple[str, UUID] | None = None
        if cursor is not None:
            name, vendor_id = decode_cursor(cursor, parts=2)
            try:
                after = (name, UUID(vendor_id))
            except ValueError as exc:
                raise ValidationFailed("The cursor is invalid.") from exc
        async with self._uow_factory() as uow:
            rows = await uow.vendors.list(
                principal.scope(),
                q=normalize_vendor_name(q) if q else None,
                limit=limit + 1,
                after=after,
            )
        page = rows[:limit]
        next_cursor = (
            encode_cursor(page[-1].normalized_name, str(page[-1].id)) if len(rows) > limit else None
        )
        return Page(items=[vendor_to_dto(row) for row in page], next_cursor=next_cursor)

    async def get(self, principal: Principal, vendor_id: UUID) -> VendorDetailDTO:
        scope = principal.scope()
        async with self._uow_factory() as uow:
            vendor = await uow.vendors.get(scope, vendor_id)
            if vendor is None:
                raise NotFound("Vendor not found.")
            documents = await uow.documents.list(scope, vendor_id=vendor_id, limit=100)
            return VendorDetailDTO(
                vendor=vendor_to_dto(vendor), documents=[document_to_dto(d) for d in documents]
            )

    async def update(
        self, principal: Principal, vendor_id: UUID, changes: dict[str, Any]
    ) -> VendorDTO:
        scope = principal.scope()
        fields = {name: value for name, value in changes.items() if name in PATCHABLE_FIELDS}
        if "metadata" in fields:
            fields["metadata_"] = fields.pop("metadata") or {}
        async with self._uow_factory() as uow:
            if "name" in fields:
                fields["name"] = clean_vendor_name(fields["name"])
                fields["normalized_name"] = normalize_vendor_name(fields["name"])
                clash = await uow.vendors.get_by_normalized_name(scope, fields["normalized_name"])
                if clash is not None and clash.id != vendor_id:
                    raise Conflict("A vendor with this name already exists.")
            try:
                vendor = await uow.vendors.update(scope, vendor_id, **fields)
            except IntegrityError:
                raise Conflict("A vendor with this name already exists.") from None
            if vendor is None:
                raise NotFound("Vendor not found.")
            await uow.commit()
            return vendor_to_dto(vendor)
