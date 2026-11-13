from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from covenantiq.core.enums import Environment, LogFormat

_SECRETS_DIR = Path("/run/secrets")

# Models whose output dimension is known; a mismatch with CIQ_EMBEDDING_DIM fails at startup.
KNOWN_EMBEDDING_DIMS: dict[str, int] = {"voyage-law-2": 1024}

_WEAK_PASSWORDS = frozenset(
    {
        "",
        "ciq",
        "password",
        "guest",
        "changeme",
        "change-me",
        "postgres",
        "redis",
        "admin",
        "secret",
    }
)

PositiveInt = Annotated[int, Field(gt=0)]


class Settings(BaseSettings):
    """All application settings. Environment variables use the CIQ_ prefix (spec §35)."""

    model_config = SettingsConfigDict(
        env_prefix="CIQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        secrets_dir=_SECRETS_DIR if _SECRETS_DIR.is_dir() else None,
        extra="ignore",
        populate_by_name=True,
    )

    env: Environment = Environment.DEV
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: LogFormat | None = None

    database_url: SecretStr
    checkpoint_db_url: SecretStr
    db_pool_size: PositiveInt = 10
    db_max_overflow: Annotated[int, Field(ge=0)] = 10
    redis_url: SecretStr
    celery_result_backend: SecretStr
    rabbitmq_url: SecretStr

    jwt_secret: SecretStr
    jwt_ttl_minutes: PositiveInt = 60
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:8501"]
    api_workers: PositiveInt = 2

    upload_dir: Path = Path("/data/uploads")
    max_upload_mb: PositiveInt = 25
    max_pages: PositiveInt = 300
    max_ingest_backlog: PositiveInt = 200
    max_active_jobs_per_user: PositiveInt = 10

    llm_provider: Literal["anthropic", "fake"] = "anthropic"
    llm_model_default: str = "claude-opus-5-5"
    llm_routes_file: Path = Path("config/llm_routes.yaml")
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")

    embedding_provider: Literal["voyage", "openai_compat", "fake"] = "voyage"
    embedding_model: str = "voyage-law-2"
    embedding_dim: PositiveInt = 1024
    embedding_batch_size: PositiveInt = 64
    rerank_provider: Literal["voyage", "fake", "none"] = "voyage"
    rerank_model: str = "rerank-2.5"
    voyage_api_key: SecretStr | None = Field(default=None, validation_alias="VOYAGE_API_KEY")
    openai_compat_base_url: str | None = None
    openai_compat_api_key: SecretStr | None = None

    chunk_target_tokens: PositiveInt = 350
    parent_max_tokens: PositiveInt = 1500
    context_max_tokens: PositiveInt = 6000
    exact_scan_threshold: PositiveInt = 5000
    rag_config_file: Path = Path("config/rag.yaml")

    session_ttl_seconds: PositiveInt = 7200
    memory_window_tokens: PositiveInt = 3000
    memory_summary_tokens: PositiveInt = 600
    stream_ttl_seconds: PositiveInt = 600
    stream_stale_seconds: PositiveInt = 300
    stream_idle_timeout: PositiveInt = 90

    analysis_max_concurrency: PositiveInt = 3
    docling_page_batch: PositiveInt = 50
    ruleset: str = "contract_baseline_v1"
    scoring_config: Path = Path("config/vendor/scoring_v1.yaml")
    rate_limits: dict[str, int] | None = None

    offline: bool = False

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("anthropic_api_key", "voyage_api_key", "openai_compat_api_key", mode="before")
    @classmethod
    def _blank_key_is_unset(cls, value: object) -> object:
        # Compose passes unset keys through as empty strings.
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode()) < 32:
            raise ValueError("must be at least 32 bytes")
        return value

    @model_validator(mode="after")
    def _resolve_and_validate(self) -> "Settings":
        if self.log_format is None:
            self.log_format = LogFormat.CONSOLE if self.env is Environment.DEV else LogFormat.JSON

        if self.offline:
            self.llm_provider = "fake"
            self.embedding_provider = "fake"
            self.rerank_provider = "fake"

        problems = [
            *self._missing_provider_keys(),
            *self._embedding_dim_problems(),
            *(self._prod_problems() if self.env is Environment.PROD else []),
        ]
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _missing_provider_keys(self) -> list[str]:
        problems: list[str] = []
        if self.llm_provider == "anthropic" and self.anthropic_api_key is None:
            problems.append("ANTHROPIC_API_KEY is required when CIQ_LLM_PROVIDER=anthropic")
        if "voyage" in (self.embedding_provider, self.rerank_provider) and (
            self.voyage_api_key is None
        ):
            problems.append("VOYAGE_API_KEY is required when a voyage provider is selected")
        if self.embedding_provider == "openai_compat" and not (
            self.openai_compat_base_url and self.openai_compat_api_key
        ):
            problems.append(
                "CIQ_OPENAI_COMPAT_BASE_URL and CIQ_OPENAI_COMPAT_API_KEY are required "
                "when CIQ_EMBEDDING_PROVIDER=openai_compat"
            )
        return problems

    def _embedding_dim_problems(self) -> list[str]:
        declared = KNOWN_EMBEDDING_DIMS.get(self.embedding_model)
        if declared is not None and declared != self.embedding_dim:
            return [
                f"CIQ_EMBEDDING_DIM={self.embedding_dim} but {self.embedding_model} "
                f"produces {declared}-dimensional vectors"
            ]
        return []

    def _prod_problems(self) -> list[str]:
        problems: list[str] = []
        for name, provider in (
            ("CIQ_LLM_PROVIDER", self.llm_provider),
            ("CIQ_EMBEDDING_PROVIDER", self.embedding_provider),
            ("CIQ_RERANK_PROVIDER", self.rerank_provider),
        ):
            if provider == "fake":
                problems.append(f"{name}=fake is not allowed in prod")

        urls = {
            "CIQ_DATABASE_URL": self.database_url,
            "CIQ_CHECKPOINT_DB_URL": self.checkpoint_db_url,
            "CIQ_REDIS_URL": self.redis_url,
            "CIQ_CELERY_RESULT_BACKEND": self.celery_result_backend,
            "CIQ_RABBITMQ_URL": self.rabbitmq_url,
        }
        for name, url in urls.items():
            password = urlsplit(url.get_secret_value()).password
            if password is None or password.lower() in _WEAK_PASSWORDS:
                problems.append(f"{name} must carry a non-default password in prod")

        if self.jwt_secret.get_secret_value().lower().startswith("change-me"):
            problems.append("CIQ_JWT_SECRET must not be the placeholder value in prod")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
