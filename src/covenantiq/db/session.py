from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from covenantiq.core.config import Settings


def create_engine(settings: Settings, *, null_pool: bool = False) -> AsyncEngine:
    """Pooled engine for the API; `null_pool=True` for Celery workers (one event loop per task)."""
    url = settings.database_url.get_secret_value()
    if null_pool:
        return create_async_engine(url, poolclass=NullPool)
    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
