"""The LangGraph checkpoint tables (spec 22.2, ADR-005).

The chat graph does not checkpoint: the durable state of a reply is its message rows. The analysis
graphs do, so a worker that dies can resume a run, and they need these tables to exist.
`covenantiq migrate` creates them next to the Alembic revisions; `setup` is idempotent.
"""

import asyncio
import re
import sys

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


def psycopg_url(url: str) -> str:
    """psycopg wants `postgresql://`; the application's URLs may name a SQLAlchemy driver."""
    return re.sub(r"^(postgres(?:ql)?)\+\w+://", r"\1://", url)


async def setup_checkpointer(url: str) -> None:
    async with AsyncPostgresSaver.from_conn_string(psycopg_url(url)) as saver:
        await saver.setup()


def setup_checkpointer_sync(url: str) -> None:
    """For the CLI. psycopg's async mode needs the selector event loop, which Windows lacks by
    default."""
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(setup_checkpointer(url), loop_factory=factory)
