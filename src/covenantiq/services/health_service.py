from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from covenantiq.db.migrations import current_revision, head_revision


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    checks: dict[str, str]


async def check_readiness(engine: AsyncEngine) -> ReadinessResult:
    """The API is ready when PostgreSQL answers and the schema is at the Alembic head.

    Redis and RabbitMQ checks are added when the API starts using them (M6, M10).
    Only coarse statuses are returned; connection errors are never echoed to callers.
    """
    checks = {"database": "unavailable", "migrations": "unknown"}
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            checks["database"] = "ok"
            revision = await current_revision(connection)
    except Exception:
        return ReadinessResult(ready=False, checks=checks)

    checks["migrations"] = "ok" if revision == head_revision() else "behind"
    return ReadinessResult(ready=all(value == "ok" for value in checks.values()), checks=checks)
