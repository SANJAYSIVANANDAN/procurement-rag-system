from collections.abc import Collection, Sequence
from uuid import UUID

from sqlalchemy import delete, func, select

from covenantiq.db.models import DocumentClause
from covenantiq.db.repositories.base import Repository, ensure_owned
from covenantiq.db.scope import OwnershipScope


class ClauseRepository(Repository):
    async def replace_for_document(
        self, scope: OwnershipScope, document_id: UUID, clauses: Sequence[DocumentClause]
    ) -> None:
        """Re-ingestion: drop the document's clauses (and their chunks, by cascade), insert anew."""
        for clause in clauses:
            ensure_owned(scope, clause)
        await self.session.execute(
            scope.apply(
                delete(DocumentClause).where(DocumentClause.document_id == document_id),
                DocumentClause,
            )
        )
        self.session.add_all(clauses)
        await self.session.flush()

    async def get_many(
        self, scope: OwnershipScope, clause_ids: Collection[UUID]
    ) -> Sequence[DocumentClause]:
        statement = scope.apply(
            select(DocumentClause).where(DocumentClause.id.in_(list(clause_ids))), DocumentClause
        )
        return (await self.session.execute(statement)).scalars().all()

    async def list_for_document(
        self,
        scope: OwnershipScope,
        document_id: UUID,
        clause_types: Collection[str] | None = None,
    ) -> Sequence[DocumentClause]:
        statement = scope.apply(
            select(DocumentClause).where(DocumentClause.document_id == document_id),
            DocumentClause,
        )
        if clause_types:
            statement = statement.where(DocumentClause.clause_types.overlap(list(clause_types)))
        statement = statement.order_by(DocumentClause.ordinal)
        return (await self.session.execute(statement)).scalars().all()

    async def clause_types_present(self, scope: OwnershipScope, document_id: UUID) -> set[str]:
        """Every clause type the chunker assigned to any clause of the document."""
        statement = scope.apply(
            select(func.unnest(DocumentClause.clause_types)).where(
                DocumentClause.document_id == document_id
            ),
            DocumentClause,
        ).distinct()
        return {str(name) for name in (await self.session.execute(statement)).scalars().all()}
