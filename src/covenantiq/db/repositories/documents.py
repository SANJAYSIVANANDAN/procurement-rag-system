from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, tuple_, update

from covenantiq.core.enums import DocumentStatus
from covenantiq.db.models import Document
from covenantiq.db.repositories.base import (
    Repository,
    cas_transition,
    ensure_owned,
    like_pattern,
)
from covenantiq.db.scope import OwnershipScope


class DocumentRepository(Repository):
    async def get(
        self, scope: OwnershipScope, document_id: UUID, *, include_deleted: bool = False
    ) -> Document | None:
        statement = scope.apply(select(Document).where(Document.id == document_id), Document)
        if not include_deleted:
            statement = statement.where(Document.deleted_at.is_(None))
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def list(
        self,
        scope: OwnershipScope,
        *,
        status: DocumentStatus | None = None,
        vendor_id: UUID | None = None,
        q: str | None = None,
        limit: int = 50,
        after: tuple[datetime, UUID] | None = None,
    ) -> Sequence[Document]:
        """Newest first. `after` is the (created_at, id) of the last row already seen."""
        statement = scope.apply(select(Document), Document).where(Document.deleted_at.is_(None))
        if status is not None:
            statement = statement.where(Document.status == status)
        if vendor_id is not None:
            statement = statement.where(Document.vendor_id == vendor_id)
        if q:
            statement = statement.where(Document.title.ilike(like_pattern(q), escape="\\"))
        if after is not None:
            statement = statement.where(tuple_(Document.created_at, Document.id) < tuple_(*after))
        statement = statement.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit)
        return (await self.session.execute(statement)).scalars().all()

    async def find_active_by_sha(self, scope: OwnershipScope, sha256: str) -> Document | None:
        statement = scope.apply(
            select(Document).where(Document.sha256 == sha256, Document.deleted_at.is_(None)),
            Document,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def add(self, scope: OwnershipScope, document: Document) -> Document:
        ensure_owned(scope, document)
        self.session.add(document)
        await self.session.flush()
        return document

    async def update(
        self, scope: OwnershipScope, document_id: UUID, **fields: Any
    ) -> Document | None:
        document = await self.get(scope, document_id)
        if document is None:
            return None
        for name, value in fields.items():
            setattr(document, name, value)
        await self.session.flush()
        return document

    async def soft_delete(self, scope: OwnershipScope, document_id: UUID) -> bool:
        statement = scope.apply(
            update(Document)
            .where(Document.id == document_id, Document.deleted_at.is_(None))
            .values(deleted_at=func.now(), status=DocumentStatus.DELETED)
            .returning(Document.id)
            .execution_options(synchronize_session=False),
            Document,
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def system_get(self, document_id: UUID) -> Document | None:
        return await self.session.get(Document, document_id)

    async def system_set_status(
        self,
        document_id: UUID,
        expected: Collection[DocumentStatus],
        new: DocumentStatus,
        **fields: Any,
    ) -> bool:
        return await cas_transition(
            self.session, Document, document_id, "status", expected, new, **fields
        )

    async def system_ids_with_status(
        self, statuses: Collection[DocumentStatus], limit: int
    ) -> Sequence[tuple[UUID, UUID]]:
        """(document id, owner id) of live documents in any of `statuses`, oldest first."""
        statement = (
            select(Document.id, Document.owner_id)
            .where(Document.status.in_(list(statuses)), Document.deleted_at.is_(None))
            .order_by(Document.created_at)
            .limit(limit)
        )
        return [(row.id, row.owner_id) for row in (await self.session.execute(statement)).all()]
