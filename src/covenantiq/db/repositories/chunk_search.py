from collections.abc import Collection, Sequence
from uuid import UUID

from covenantiq.db.repositories.base import Repository
from covenantiq.db.scope import OwnershipScope


class ChunkSearchRepository(Repository):
    """Dense and lexical retrieval over document_chunks. Implemented in Milestone 9.

    Both searches must filter on owner_id and on the allowed document ids (spec 15.7).
    """

    async def dense_search(
        self,
        scope: OwnershipScope,
        vector: Sequence[float],
        *,
        document_ids: Collection[UUID],
        k: int,
    ) -> Sequence[tuple[UUID, float]]:
        raise NotImplementedError("dense search arrives in Milestone 9")

    async def lexical_search(
        self,
        scope: OwnershipScope,
        query_text: str,
        *,
        document_ids: Collection[UUID],
        k: int,
    ) -> Sequence[tuple[UUID, float]]:
        raise NotImplementedError("lexical search arrives in Milestone 9")
