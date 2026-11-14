"""How the engine reaches the database in production and in tests."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from covenantiq.db.repositories.chunk_search import ChunkSearchRepository
from covenantiq.db.uow import UnitOfWork


def search_session_factory(
    uow_factory: Callable[[], UnitOfWork],
) -> Callable[[], AbstractAsyncContextManager[ChunkSearchRepository]]:
    """A fresh, read-only unit of work per call; it is never committed, so every `SET LOCAL`
    and every query ends with the call."""

    @asynccontextmanager
    async def session() -> AsyncIterator[ChunkSearchRepository]:
        async with uow_factory() as uow:
            yield uow.chunk_search

    return session
