"""Integration fixtures. They need a PostgreSQL with citext, pg_trgm and pgvector, supplied through
CIQ_TEST_DATABASE_URL (postgresql+asyncpg://...). Without it every integration test is skipped.

The schema is migrated to head once per session if it is not already there, and all rows are
truncated after every test.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from covenantiq.core.config import get_settings
from covenantiq.db.migrations import alembic_config, head_revision
from covenantiq.db.repositories import chunks as chunk_repository
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.session import create_session_factory
from covenantiq.db.uow import UnitOfWork

if TYPE_CHECKING:
    from redis.asyncio import Redis

ENV = {
    "CIQ_ENV": "test",
    "CIQ_OFFLINE": "1",
    "CIQ_CHECKPOINT_DB_URL": "postgresql://ciq:ciq@localhost/unused",
    "CIQ_REDIS_URL": "redis://localhost:6379/0",
    "CIQ_CELERY_RESULT_BACKEND": "redis://localhost:6379/1",
    "CIQ_RABBITMQ_URL": "amqp://ciq:ciq@localhost:5672/covenantiq",
    "CIQ_JWT_SECRET": "x" * 40,
}


def _scalar(url: str, sql: str) -> Any:
    async def go() -> Any:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                return (await connection.execute(text(sql))).scalar_one_or_none()
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("CIQ_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set CIQ_TEST_DATABASE_URL to run integration tests")
    return url


@pytest.fixture(scope="session")
def db_env(database_url: str) -> Iterator[None]:
    """Point Settings (and therefore Alembic) at the test database."""
    patch = pytest.MonkeyPatch()
    for name, value in {**ENV, "CIQ_DATABASE_URL": database_url}.items():
        patch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    patch.undo()


@pytest.fixture(scope="session")
def pgvector_available(database_url: str) -> bool:
    return bool(
        _scalar(database_url, "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
    )


@pytest.fixture(scope="session")
def pgvector_installed(database_url: str) -> bool:
    return bool(_scalar(database_url, "SELECT 1 FROM pg_extension WHERE extname = 'vector'"))


@pytest.fixture(scope="session", autouse=True)
def schema(database_url: str, db_env: None) -> None:
    """Bring the schema to head unless it already is (so a pre-provisioned database works)."""
    has_version_table = _scalar(database_url, "SELECT to_regclass('alembic_version')") is not None
    revision = (
        _scalar(database_url, "SELECT version_num FROM alembic_version")
        if has_version_table
        else None
    )
    if revision != head_revision():
        command.upgrade(alembic_config(), "head")


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    yield engine
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE users CASCADE"))
    await engine.dispose()


@pytest.fixture
def uow_factory(engine: AsyncEngine) -> Callable[[], UnitOfWork]:
    session_factory = create_session_factory(engine)
    return lambda: UnitOfWork(session_factory)


@pytest.fixture
def require_pgvector(pgvector_installed: bool) -> None:
    if not pgvector_installed:
        pytest.skip("pgvector extension is not installed in the test database")


@pytest.fixture(autouse=True)
def vector_writes(
    pgvector_installed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:  # pragma: no cover - which branch runs depends on the database
    if pgvector_installed:
        return

    async def set_embeddings(
        self: chunk_repository.ChunkRepository,
        scope: OwnershipScope,
        batch: Sequence[tuple[UUID, Sequence[float]]],
        embedding_model: str,
    ) -> None:
        for chunk_id, vector in batch:
            await self.session.execute(
                text(
                    "UPDATE document_chunks SET embedding = CAST(:vector AS float4[]), "
                    "embedding_model = :model WHERE id = :id AND owner_id = :owner"
                ),
                {
                    "vector": list(vector),
                    "model": embedding_model,
                    "id": chunk_id,
                    "owner": scope.owner_id,
                },
            )

    monkeypatch.setattr(chunk_repository.ChunkRepository, "set_embeddings", set_embeddings)


@pytest.fixture(scope="session")
def redis_url() -> str:
    """A real Redis for the tests that need one (CIQ_TEST_REDIS_URL); skipped without it."""
    url = os.environ.get("CIQ_TEST_REDIS_URL")
    if not url:
        pytest.skip("set CIQ_TEST_REDIS_URL to run the Redis integration tests")
    return url


@pytest.fixture
def redis_env() -> str:
    """A key namespace used by one test only, so tests cannot see each other's keys."""
    return f"t{uuid4().hex[:10]}"


@pytest_asyncio.fixture
async def redis_client(redis_url: str, redis_env: str) -> AsyncIterator["Redis"]:
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url)
    yield client
    keys = [key async for key in client.scan_iter(match=f"ciq:{redis_env}:*", count=200)]
    if keys:
        await client.delete(*keys)
    await client.aclose()
