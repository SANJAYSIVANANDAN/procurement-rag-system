from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from covenantiq.api.container import Container
from covenantiq.core.config import get_settings
from covenantiq.core.security import PasswordHasher
from covenantiq.db.session import create_session_factory
from covenantiq.db.uow import UnitOfWork
from covenantiq.services import health_service
from covenantiq.services.auth_service import AuthService
from covenantiq.services.rate_limiter import RateLimiter
from tests.fakes import FakeRedis

PASSWORD = "correct horse battery staple"

# Cheap Argon2 so the suite stays fast; the production defaults are unit-tested separately.
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost_kib=1024, parallelism=1)

# A frozen clock keeps every request inside one rate-limit window.
FROZEN_NOW = 1_000_020.0


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def container(engine: AsyncEngine, redis: FakeRedis) -> Container:
    settings = get_settings()
    session_factory = create_session_factory(engine)
    return Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis.as_redis(),
        rate_limiter=RateLimiter(
            redis.as_redis(), env="test", overrides=settings.rate_limits, clock=lambda: FROZEN_NOW
        ),
        auth_service=AuthService(
            lambda: UnitOfWork(session_factory),
            FAST_HASHER,
            jwt_secret=settings.jwt_secret.get_secret_value(),
            jwt_ttl_minutes=settings.jwt_ttl_minutes,
        ),
    )


@pytest_asyncio.fixture
async def client(container: Container) -> AsyncIterator[httpx.AsyncClient]:
    # Imported here: the module builds its app at import time and needs a valid environment.
    from covenantiq.api.main import create_app

    app = create_app(container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def broker_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_service, "_broker_reachable", lambda _url: True)


Register = Callable[..., Any]


@pytest.fixture
def register(client: httpx.AsyncClient) -> Register:
    async def go(
        email: str | None = None, password: str = PASSWORD, **extra: Any
    ) -> dict[str, Any]:
        email = email or f"{uuid4().hex[:10]}@example.com"
        response = await client.post(
            "/api/v1/auth/register", json={"email": email, "password": password, **extra}
        )
        assert response.status_code == 201, response.text
        return {"email": email, "password": password, **response.json()}

    return go


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
