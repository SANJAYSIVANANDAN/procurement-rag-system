from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from covenantiq.core.config import Settings
from covenantiq.core.security import PasswordHasher
from covenantiq.db.session import create_engine, create_session_factory
from covenantiq.db.uow import UnitOfWork
from covenantiq.services.auth_service import AuthService
from covenantiq.services.rate_limiter import RateLimiter

REDIS_TIMEOUT_SECONDS = 2


@dataclass
class Container:
    """Everything the API process shares: built once at startup, read by dependencies."""

    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    rate_limiter: RateLimiter
    auth_service: AuthService

    async def aclose(self) -> None:
        await self.redis.aclose()
        await self.engine.dispose()


def build_container(settings: Settings) -> Container:
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_timeout=REDIS_TIMEOUT_SECONDS,
        socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
    )
    return Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis,
        rate_limiter=RateLimiter(redis, env=settings.env.value, overrides=settings.rate_limits),
        auth_service=AuthService(
            lambda: UnitOfWork(session_factory),
            PasswordHasher(),
            jwt_secret=settings.jwt_secret.get_secret_value(),
            jwt_ttl_minutes=settings.jwt_ttl_minutes,
        ),
    )
