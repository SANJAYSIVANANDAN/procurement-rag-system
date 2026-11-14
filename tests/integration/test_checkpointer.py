"""`covenantiq migrate` creates the LangGraph checkpoint tables (spec 22.2)."""

import asyncio
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from covenantiq.graphs.checkpointing import psycopg_url, setup_checkpointer_sync

CHECKPOINT_TABLES = (
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
    "checkpoint_migrations",
)


def test_a_driver_suffix_is_removed_for_psycopg() -> None:
    assert psycopg_url("postgresql+asyncpg://u:p@h:5/d") == "postgresql://u:p@h:5/d"
    assert psycopg_url("postgresql://u:p@h:5/d") == "postgresql://u:p@h:5/d"
    assert psycopg_url("postgres+psycopg://u:p@h/d") == "postgres://u:p@h/d"


async def _run(database_url: str, sql: str) -> list[str]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(sql))
            return [row[0] for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest.fixture
def clean_checkpoint_tables(database_url: str) -> Iterator[None]:
    """The tables are not part of the Alembic schema, so the migrations test must not meet them."""
    yield
    asyncio.run(_run(database_url, f"DROP TABLE IF EXISTS {', '.join(CHECKPOINT_TABLES)} CASCADE"))


@pytest.mark.usefixtures("clean_checkpoint_tables")
def test_setup_creates_the_tables_and_can_run_again(database_url: str) -> None:
    plain = psycopg_url(database_url)
    setup_checkpointer_sync(plain)
    setup_checkpointer_sync(plain)  # idempotent: `migrate` runs on every deploy

    found = asyncio.run(
        _run(
            database_url,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name LIKE 'checkpoint%'",
        )
    )
    assert {"checkpoints", "checkpoint_writes", "checkpoint_blobs"} <= set(found)
