"""Integration fixtures. They need a PostgreSQL with citext, pg_trgm and pgvector, supplied through
CIQ_TEST_DATABASE_URL (postgresql+asyncpg://...). Without it every integration test is skipped.

The schema is migrated to head once per session if it is not already there, and all rows are
truncated after every test.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from covenantiq.core.config import get_settings
from covenantiq.db.migrations import alembic_config, head_revision
from covenantiq.db.session import create_session_factory
from covenantiq.db.uow import UnitOfWork

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
