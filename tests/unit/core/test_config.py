import pytest
from pydantic import ValidationError

from covenantiq.core.config import Settings
from covenantiq.core.enums import Environment, LogFormat


def load() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_loads_with_defaults(valid_env: dict[str, str]) -> None:
    settings = load()
    assert settings.env is Environment.TEST
    assert settings.max_upload_mb == 25
    assert settings.cors_origins == ["http://localhost:8501"]
    assert settings.jwt_secret.get_secret_value() == valid_env["CIQ_JWT_SECRET"]


def test_secrets_are_masked_in_dumps(valid_env: dict[str, str]) -> None:
    dumped = str(load().model_dump(mode="json"))
    assert valid_env["CIQ_JWT_SECRET"] not in dumped
    assert "ciq:ciq@" not in dumped


@pytest.mark.parametrize(
    "missing",
    ["CIQ_DATABASE_URL", "CIQ_REDIS_URL", "CIQ_RABBITMQ_URL", "CIQ_JWT_SECRET"],
)
def test_required_variables(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing)
    with pytest.raises(ValidationError):
        load()


def test_short_jwt_secret_is_rejected(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIQ_JWT_SECRET", "too-short")
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        load()


def test_cors_origins_are_comma_separated(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIQ_CORS_ORIGINS", "http://a.test, http://b.test")
    assert load().cors_origins == ["http://a.test", "http://b.test"]


def test_log_format_defaults_by_environment(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIQ_ENV", "dev")
    assert load().log_format is LogFormat.CONSOLE
    monkeypatch.setenv("CIQ_ENV", "test")
    assert load().log_format is LogFormat.JSON


def test_offline_forces_fake_providers(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIQ_OFFLINE", "1")
    settings = load()
    assert (settings.llm_provider, settings.embedding_provider, settings.rerank_provider) == (
        "fake",
        "fake",
        "fake",
    )


def test_real_providers_need_their_api_keys(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIQ_OFFLINE", "0")
    with pytest.raises(ValidationError) as exc:
        load()
    message = str(exc.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "VOYAGE_API_KEY" in message


def test_blank_api_keys_count_as_unset(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIQ_OFFLINE", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("VOYAGE_API_KEY", "  ")
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY is required"):
        load()


def test_embedding_dimension_must_match_known_model(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIQ_EMBEDDING_DIM", "768")
    with pytest.raises(ValidationError, match="1024-dimensional"):
        load()


@pytest.fixture
def prod_env(valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIQ_ENV", "prod")
    monkeypatch.setenv("CIQ_OFFLINE", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    strong = {
        "CIQ_DATABASE_URL": "postgresql+asyncpg://ciq:S0me-Strong-Pw@db:5432/covenantiq",
        "CIQ_CHECKPOINT_DB_URL": "postgresql://ciq:S0me-Strong-Pw@db:5432/covenantiq",
        "CIQ_REDIS_URL": "redis://:S0me-Strong-Pw@redis:6379/0",
        "CIQ_CELERY_RESULT_BACKEND": "redis://:S0me-Strong-Pw@redis:6379/1",
        "CIQ_RABBITMQ_URL": "amqp://ciq:S0me-Strong-Pw@rabbitmq:5672/covenantiq",
    }
    for name, value in strong.items():
        monkeypatch.setenv(name, value)


@pytest.mark.usefixtures("prod_env")
def test_prod_accepts_strong_configuration() -> None:
    assert load().env is Environment.PROD


@pytest.mark.usefixtures("prod_env")
@pytest.mark.parametrize(
    "variable", ["CIQ_LLM_PROVIDER", "CIQ_EMBEDDING_PROVIDER", "CIQ_RERANK_PROVIDER"]
)
def test_prod_rejects_fake_providers(monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
    monkeypatch.setenv(variable, "fake")
    with pytest.raises(ValidationError, match="not allowed in prod"):
        load()


@pytest.mark.usefixtures("prod_env")
def test_prod_rejects_default_service_passwords(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIQ_REDIS_URL", "redis://ciq:ciq@localhost:6379/0")
    with pytest.raises(ValidationError, match="CIQ_REDIS_URL"):
        load()


@pytest.mark.usefixtures("prod_env")
def test_prod_rejects_placeholder_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIQ_JWT_SECRET", "change-me-" + "x" * 30)
    with pytest.raises(ValidationError, match="placeholder"):
        load()
