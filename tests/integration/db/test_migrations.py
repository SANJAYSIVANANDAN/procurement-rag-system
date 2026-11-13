"""Migration round trip. These need a database where the pgvector extension is available, because
migration 0001 creates it; they are skipped otherwise. Sync tests: Alembic's async env runs its own
event loop, so it cannot be called from inside an async test.
"""

import asyncio
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from covenantiq.db.base import Base
from covenantiq.db.migrations import alembic_config, head_revision

EXPECTED_TABLES = set(Base.metadata.tables) | {"alembic_version"}


@pytest.fixture(autouse=True)
def _needs_pgvector(pgvector_available: bool) -> None:
    if not pgvector_available:
        pytest.skip("pgvector is not available in the test database")


def _query(url: str, sql: str) -> list[Any]:
    async def go() -> list[Any]:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                return list((await connection.execute(text(sql))).scalars().all())
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _tables(url: str) -> set[str]:
    return set(
        _query(url, "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    )


def test_upgrade_downgrade_upgrade_is_clean(database_url: str) -> None:
    config = alembic_config()

    command.downgrade(config, "base")
    assert _tables(database_url) <= {"alembic_version"}
    assert _query(database_url, "SELECT typname FROM pg_type WHERE typtype = 'e'") == []
    assert (
        _query(
            database_url,
            "SELECT extname FROM pg_extension WHERE extname IN ('vector','citext','pg_trgm')",
        )
        == []
    )

    command.upgrade(config, "head")
    assert _tables(database_url) == EXPECTED_TABLES
    assert _query(database_url, "SELECT version_num FROM alembic_version") == [head_revision()]
    assert sorted(
        _query(
            database_url,
            "SELECT extname FROM pg_extension WHERE extname IN ('vector','citext','pg_trgm')",
        )
    ) == ["citext", "pg_trgm", "vector"]


def test_models_match_the_migrated_schema(database_url: str) -> None:
    async def diff() -> list[Any]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:

                def compare(sync_connection: Any) -> list[Any]:
                    context = MigrationContext.configure(
                        sync_connection, opts={"compare_type": True}
                    )
                    return compare_metadata(context, Base.metadata)

                return await connection.run_sync(compare)
        finally:
            await engine.dispose()

    assert asyncio.run(diff()) == []
