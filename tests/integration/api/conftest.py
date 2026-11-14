from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from celery import Celery
from sqlalchemy.ext.asyncio import AsyncEngine

from covenantiq.analysis.contract.rules_loader import get_ruleset
from covenantiq.analysis.vendor.config_schema import get_vendor_config
from covenantiq.api.container import Container
from covenantiq.core.config import get_settings
from covenantiq.core.security import PasswordHasher
from covenantiq.db.session import create_session_factory
from covenantiq.db.uow import UnitOfWork
from covenantiq.ingestion.storage import LocalFileStorage
from covenantiq.rag.factory import build_retrieval_engine
from covenantiq.services import health_service
from covenantiq.services.auth_service import AuthService
from covenantiq.services.contract_analysis_service import ContractAnalysisService
from covenantiq.services.dlq_service import DlqService
from covenantiq.services.document_service import DocumentService
from covenantiq.services.job_service import JobService
from covenantiq.services.rate_limiter import RateLimiter
from covenantiq.services.reembed_service import ReembedService
from covenantiq.services.sweep_service import SweepService
from covenantiq.services.vendor_assessment_service import VendorAssessmentService
from covenantiq.services.vendor_comparison_service import VendorComparisonService
from covenantiq.services.vendor_service import VendorService
from tests.fakes import FakeRedis, RecordingDispatcher
from tests.integration.chat.stack import make_parts
from tests.unit.graphs.harness import weak_retrieval

PASSWORD = "correct horse battery staple"

# Cheap Argon2 so the suite stays fast; the production defaults are unit-tested separately.
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost_kib=1024, parallelism=1)

# A frozen clock keeps every request inside one rate-limit window.
FROZEN_NOW = 1_000_020.0


@pytest.fixture
def max_upload_mb() -> int:
    """Override in a test module to exercise a smaller upload limit cheaply."""
    return 25


@pytest.fixture
def max_active_jobs() -> int:
    return 10


@pytest.fixture(autouse=True)
def _settings_overrides(
    monkeypatch: pytest.MonkeyPatch, max_upload_mb: int, max_active_jobs: int, storage_root: Path
) -> Iterator[None]:
    monkeypatch.setenv("CIQ_MAX_UPLOAD_MB", str(max_upload_mb))
    monkeypatch.setenv("CIQ_MAX_ACTIVE_JOBS_PER_USER", str(max_active_jobs))
    monkeypatch.setenv("CIQ_UPLOAD_DIR", str(storage_root))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    return tmp_path / "uploads"


class Backlog:
    """A controllable stand-in for the queue-depth probe."""

    def __init__(self) -> None:
        self.depth = 0

    async def __call__(self) -> int:
        return self.depth


@pytest.fixture
def backlog() -> Backlog:
    return Backlog()


@pytest.fixture
def container(
    engine: AsyncEngine,
    redis: FakeRedis,
    dispatcher: RecordingDispatcher,
    storage_root: Path,
    backlog: Backlog,
) -> Container:
    settings = get_settings()
    session_factory = create_session_factory(engine)

    def uow_factory() -> UnitOfWork:
        return UnitOfWork(session_factory)

    storage = LocalFileStorage(storage_root)
    job_service = JobService(uow_factory)
    celery_app = Celery("test", broker="memory://")
    # Chat needs a real Redis (Lua locks, lists); tests that use it override `container` with one.
    # Here it only has to exist, so the rest of the API is built exactly as it runs.
    chat, conversations = make_parts(
        uow_factory,
        redis.as_redis(),
        "test",
        weak_retrieval(),  # type: ignore[arg-type]
    )
    contract_analysis = ContractAnalysisService(uow_factory, dispatcher, job_service, get_ruleset())
    vendor_config = get_vendor_config()
    return Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis.as_redis(),
        rate_limiter=RateLimiter(
            redis.as_redis(), env="test", overrides=settings.rate_limits, clock=lambda: FROZEN_NOW
        ),
        auth_service=AuthService(
            uow_factory,
            FAST_HASHER,
            jwt_secret=settings.jwt_secret.get_secret_value(),
            jwt_ttl_minutes=settings.jwt_ttl_minutes,
        ),
        storage=storage,
        dispatcher=dispatcher,
        document_service=DocumentService(
            uow_factory,
            storage,
            dispatcher,
            max_upload_bytes=settings.max_upload_mb * 1024 * 1024,
            max_pages=settings.max_pages,
            max_active_jobs=settings.max_active_jobs_per_user,
            backlog=backlog,
            max_backlog=settings.max_ingest_backlog,
        ),
        conversation_service=conversations,
        vendor_service=VendorService(uow_factory),
        job_service=job_service,
        sweep_service=SweepService(job_service, dispatcher, redis.as_redis(), env="test"),
        celery_app=celery_app,
        dlq_service=DlqService(celery_app.connection_for_write, job_service, dispatcher),
        reembed_service=ReembedService(uow_factory, dispatcher),
        chat_service=chat.service,
        contract_analysis_service=contract_analysis,
        vendor_assessment_service=VendorAssessmentService(
            uow_factory, dispatcher, job_service, contract_analysis, get_ruleset(), vendor_config
        ),
        vendor_comparison_service=VendorComparisonService(
            uow_factory, dispatcher, job_service, vendor_config
        ),
        memory=chat.memory,
        retrieval=build_retrieval_engine(settings, uow_factory),
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


class Session:
    """A registered, logged-in user with an authorization header ready to use."""

    def __init__(self, user: dict[str, Any], token: str) -> None:
        self.user = user
        self.id: str = user["id"]
        self.headers = bearer(token)


@pytest.fixture
def make_session(client: httpx.AsyncClient, register: Register) -> Callable[[], Any]:
    async def go() -> Session:
        user = await register()
        return Session(user, await login(client, user["email"]))

    return go
