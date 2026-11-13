import os
from collections.abc import Iterator

import pytest

from covenantiq.core.config import get_settings

VALID_ENV = {
    "CIQ_ENV": "test",
    "CIQ_DATABASE_URL": "postgresql+asyncpg://ciq:ciq@localhost:5432/covenantiq_test",
    "CIQ_CHECKPOINT_DB_URL": "postgresql://ciq:ciq@localhost:5432/covenantiq_test",
    "CIQ_REDIS_URL": "redis://localhost:6379/0",
    "CIQ_CELERY_RESULT_BACKEND": "redis://localhost:6379/1",
    "CIQ_RABBITMQ_URL": "amqp://ciq:ciq@localhost:5672/covenantiq",
    "CIQ_JWT_SECRET": "x" * 40,
    "CIQ_OFFLINE": "1",
}


@pytest.fixture
def valid_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """A complete, valid environment with fake providers, isolated from the developer's shell."""
    for name in [n for n in os.environ if n.startswith("CIQ_")]:
        monkeypatch.delenv(name)
    for name in ("ANTHROPIC_API_KEY", "VOYAGE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in VALID_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield dict(VALID_ENV)
    get_settings.cache_clear()
