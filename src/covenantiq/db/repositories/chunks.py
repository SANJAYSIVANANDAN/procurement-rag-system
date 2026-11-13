from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, select, update

from covenantiq.db.models import DocumentChunk
from covenantiq.db.repositories.base import Repository, ensure_owned
from covenantiq.db.scope import OwnershipScope


class ChunkRepository(Repository):
    async def bulk_insert(self, scope: OwnershipScope, chunks: Sequence[DocumentChunk]) -> None:
        for chunk in chunks:
            ensure_owned(scope, chunk)
        self.session.add_all(chunks)
        await self.session.flush()

    async def pending_embedding_batch(
        self, scope: OwnershipScope, document_id: UUID, limit: int
    ) -> Sequence[DocumentChunk]:
        statement = scope.apply(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id, DocumentChunk.embedding.is_(None))
            .order_by(DocumentChunk.ordinal)
            .limit(limit),
            DocumentChunk,
        )
        return (await self.session.execute(statement)).scalars().all()

    async def set_embeddings(
        self,
        scope: OwnershipScope,
        batch: Sequence[tuple[UUID, Sequence[float]]],
        embedding_model: str,
    ) -> None:
        for chunk_id, vector in batch:
            await self.session.execute(
                scope.apply(
                    update(DocumentChunk)
                    .where(DocumentChunk.id == chunk_id)
                    .values(embedding=list(vector), embedding_model=embedding_model)
                    .execution_options(synchronize_session=False),
                    DocumentChunk,
                )
            )

    async def delete_for_document(self, scope: OwnershipScope, document_id: UUID) -> int:
        result = await self.session.execute(
            scope.apply(
                delete(DocumentChunk).where(DocumentChunk.document_id == document_id),
                DocumentChunk,
            )
        )
        return int(result.rowcount)  # type: ignore[attr-defined]
