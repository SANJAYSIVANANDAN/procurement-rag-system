from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select, delete, func, select, update

from covenantiq.db.models import DocumentChunk
from covenantiq.db.repositories.base import Repository, ensure_owned
from covenantiq.db.scope import OwnershipScope

EMBEDDING_ERROR_KEY = "embedding_error"


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
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.embedding.is_(None),
                DocumentChunk.metadata_[EMBEDDING_ERROR_KEY].as_string().is_(None),
            )
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

    async def mark_embedding_error(
        self, scope: OwnershipScope, chunk_id: UUID, message: str
    ) -> None:
        """Record that the provider refused this chunk, so it is not retried forever and the
        document can still become ready if only a few chunks are affected."""
        await self.session.execute(
            scope.apply(
                update(DocumentChunk)
                .where(DocumentChunk.id == chunk_id)
                .values(
                    metadata_=DocumentChunk.metadata_.concat({EMBEDDING_ERROR_KEY: message[:300]})
                )
                .execution_options(synchronize_session=False),
                DocumentChunk,
            )
        )

    async def embedding_counts(
        self, scope: OwnershipScope, document_id: UUID
    ) -> tuple[int, int, int]:
        """(total, embedded, refused) chunks of a document."""
        counts = cast(
            "Select[Any]",
            select(
                func.count(),
                func.count(DocumentChunk.embedding),
                func.count().filter(
                    DocumentChunk.metadata_[EMBEDDING_ERROR_KEY].as_string().is_not(None)
                ),
            ).where(DocumentChunk.document_id == document_id),
        )
        statement = scope.apply(counts, DocumentChunk)
        total, embedded, refused = (await self.session.execute(statement)).one()
        return int(total), int(embedded), int(refused)

    async def clear_embeddings(self, scope: OwnershipScope, document_id: UUID) -> int:
        """Forget every vector (and refusal) of a document, so it is embedded again, for
        example after the embedding model changed."""
        result = await self.session.execute(
            scope.apply(
                update(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
                .values(
                    embedding=None,
                    embedding_model=None,
                    metadata_=DocumentChunk.metadata_.op("-")(EMBEDDING_ERROR_KEY),
                )
                .execution_options(synchronize_session=False),
                DocumentChunk,
            )
        )
        return int(result.rowcount)  # type: ignore[attr-defined]
