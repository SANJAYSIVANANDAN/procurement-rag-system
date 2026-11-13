import asyncio
from dataclasses import dataclass

from kombu.connection import Connection
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from covenantiq.db.migrations import current_revision, head_revision

CHECK_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    checks: dict[str, str]


async def _check_database(engine: AsyncEngine) -> tuple[str, str]:
    """Returns (database status, migrations status)."""
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            revision = await current_revision(connection)
    except Exception:
        return "unavailable", "unknown"
    return "ok", "ok" if revision == head_revision() else "behind"


async def _check_redis(redis: Redis) -> str:
    try:
        await asyncio.wait_for(redis.ping(), CHECK_TIMEOUT_SECONDS)
    except Exception:
        return "unavailable"
    return "ok"


def _broker_reachable(broker_url: str) -> bool:
    connection = Connection(broker_url, connect_timeout=2)
    try:
        connection.connect()
        return bool(connection.connected)
    except Exception:
        return False
    finally:
        connection.release()


async def _check_rabbitmq(broker_url: str) -> str:
    try:
        reachable = await asyncio.wait_for(
            asyncio.to_thread(_broker_reachable, broker_url), CHECK_TIMEOUT_SECONDS
        )
    except Exception:
        return "unavailable"
    return "ok" if reachable else "unavailable"


async def check_readiness(engine: AsyncEngine, redis: Redis, broker_url: str) -> ReadinessResult:
    """Ready when PostgreSQL answers, the schema is at the Alembic head, Redis answers PING and
    RabbitMQ accepts a connection. Only coarse statuses are returned; errors are never echoed."""
    (database, migrations), redis_status, rabbitmq_status = await asyncio.gather(
        _check_database(engine), _check_redis(redis), _check_rabbitmq(broker_url)
    )
    checks = {
        "database": database,
        "migrations": migrations,
        "redis": redis_status,
        "rabbitmq": rabbitmq_status,
    }
    return ReadinessResult(ready=all(value == "ok" for value in checks.values()), checks=checks)
