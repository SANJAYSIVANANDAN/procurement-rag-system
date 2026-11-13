import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from covenantiq.services import health_service
from covenantiq.services.health_service import check_readiness
from tests.fakes import FakeRedis

BROKER = "amqp://ciq:ciq@localhost:5672/covenantiq"


@pytest.fixture(autouse=True)
def broker_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_service, "_broker_reachable", lambda _url: True)


def redis_client(*, down: bool = False) -> Redis:
    return FakeRedis(down=down).as_redis()


async def test_ready_when_everything_is_up_and_schema_is_at_head(engine: AsyncEngine) -> None:
    result = await check_readiness(engine, redis_client(), BROKER)
    assert result.ready
    assert result.checks == {
        "database": "ok",
        "migrations": "ok",
        "redis": "ok",
        "rabbitmq": "ok",
    }


async def test_behind_when_the_recorded_revision_is_not_head(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        head = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
        await connection.execute(text("UPDATE alembic_version SET version_num = '0004'"))
    try:
        result = await check_readiness(engine, redis_client(), BROKER)
        assert not result.ready
        assert result.checks["migrations"] == "behind"
        assert result.checks["database"] == "ok"
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :head"), {"head": head}
            )


async def test_unavailable_database_is_reported_without_details() -> None:
    dead = create_async_engine("postgresql+asyncpg://nobody:nope@127.0.0.1:1/none")
    try:
        result = await check_readiness(dead, redis_client(), BROKER)
    finally:
        await dead.dispose()
    assert not result.ready
    assert result.checks["database"] == "unavailable"
    assert result.checks["migrations"] == "unknown"


async def test_each_dependency_is_reported_separately(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await check_readiness(engine, redis_client(down=True), BROKER)
    assert not result.ready
    assert (result.checks["redis"], result.checks["database"]) == ("unavailable", "ok")

    monkeypatch.setattr(health_service, "_broker_reachable", lambda _url: False)
    result = await check_readiness(engine, redis_client(), BROKER)
    assert not result.ready
    assert (result.checks["rabbitmq"], result.checks["redis"]) == ("unavailable", "ok")
