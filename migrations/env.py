import asyncio
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import covenantiq.db.models  # noqa: F401  (registers every table on Base.metadata)
from covenantiq.core.config import get_settings
from covenantiq.db.base import Base

target_metadata = Base.metadata

# Created and versioned by the LangGraph PostgreSQL checkpointer, not by us.
CHECKPOINTER_TABLES = {
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
}


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    return not (type_ == "table" and name in CHECKPOINTER_TABLES)


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of connecting (`alembic upgrade head --sql`)."""
    context.configure(
        url=get_settings().database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(get_settings().database_url.get_secret_value(), poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
