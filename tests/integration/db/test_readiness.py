from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from covenantiq.services.health_service import check_readiness


async def test_ready_when_schema_is_at_head(engine: AsyncEngine) -> None:
    result = await check_readiness(engine)
    assert result.ready
    assert result.checks == {"database": "ok", "migrations": "ok"}


async def test_behind_when_the_recorded_revision_is_not_head(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        head = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
        await connection.execute(text("UPDATE alembic_version SET version_num = '0004'"))
    try:
        result = await check_readiness(engine)
        assert not result.ready
        assert result.checks == {"database": "ok", "migrations": "behind"}
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num = :head"), {"head": head}
            )


async def test_unavailable_database_is_reported_without_details() -> None:
    dead = create_async_engine("postgresql+asyncpg://nobody:nope@127.0.0.1:1/none")
    try:
        result = await check_readiness(dead)
    finally:
        await dead.dispose()
    assert not result.ready
    assert result.checks["database"] == "unavailable"
