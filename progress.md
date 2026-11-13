# CovenantIQ — Progress

Source of truth: [PROJECT_SPEC.md](PROJECT_SPEC.md). Milestones are defined in §36.

## Status

| Milestone | Status |
|---|---|
| M1 — Repository setup and architecture skeleton | Done and committed |
| M2 — Docker infrastructure | Code complete and committed; Docker run not yet verified (Docker not installed) |
| M3–M19 | Not started |

## Repository cleanup (before M1)

- Removed all legacy prototype files (Flask/ChromaDB app, Cloudflare scripts, `cloudflared.exe`, etc.).
- Kept only `PROJECT_SPEC.md` and `doculens-ai-reference.md`.
- Committed as `e2031cd` ("replacing the older architecture with a better one").

## M1 — Repository setup and architecture skeleton

### Tooling
- `pyproject.toml`: uv-managed project, `src` layout, hatchling build, `covenantiq` CLI entry point. Ruff, mypy (strict) and pytest (`asyncio_mode=auto`) configured.
- `Makefile`: `install`, `lint`, `format`, `typecheck`, `imports`, `test`, `check`.
- `.pre-commit-config.yaml`, `.gitignore`, `.dockerignore`, `.env.example`, `.python-version` (3.12).
- `.github/workflows/ci.yml`: lint job (ruff, format check, mypy, import-linter) and unit job (pytest, 80% coverage gate).

### Package skeleton
- `src/covenantiq/` contains an empty package for every module in spec §31: `core`, `db`, `ai`, `ingestion`, `rag`, `memory`, `analysis`, `graphs`, `services`, `streaming`, `workers`, `api` and their subpackages.

### Core implementation (`src/covenantiq/core/`)
- `config.py`: `Settings` (pydantic-settings, `CIQ_` prefix) covering the spec §35 variables.
  - Secrets use `SecretStr`.
  - Validation: JWT secret at least 32 bytes; provider API keys required for selected providers; embedding dimension must match known models; `prod` rejects fake providers, default or missing service passwords and a placeholder JWT secret.
  - `CIQ_OFFLINE=1` forces fake providers; log format defaults to console in dev and JSON elsewhere.
  - `get_settings()` is cached.
- `logging.py`: structlog configured through stdlib so uvicorn and Celery logs share the pipeline.
  - `redact_sensitive` masks sensitive keys recursively (`password`, `token`, `content`, `quote`, ...) and keys ending in `_password`, `_token`, `_secret` or `_api_key`.
  - `bind_context` and `clear_context` attach `request_id`, `user_id`, `conversation_id`, `job_id` and `task_id` to log lines.
  - The handler resolves `sys.stdout` at emit time.
- `errors.py`: `ErrorCode` enum and one exception class per code from the spec's API error table, with `to_dict()`. HTTP mapping is deferred to the API layer.
- `ids.py`: thread-safe, monotonic `uuid7()` (Python 3.12 has no built-in).
- `enums.py`: `Environment`, `LogFormat`.
- `cli.py`: `covenantiq version` and `covenantiq check-config` (reports config problems without printing values).

### Architecture enforcement
- `.importlinter`: 13 contracts encoding the "must not import" column of spec §8.2.

### Docs
- `docs/adr/ADR-001.md` to `ADR-016.md`, generated from spec §7.
- `README.md` with a quick-start placeholder.

### Tests (34 passing, 97% coverage)
- `tests/unit/core/`: config validation (missing keys, short JWT secret, prod rejections, offline mode), log redaction and context, UUIDv7 monotonicity, error codes.
- `tests/unit/test_cli.py`: help, version, `check-config` success and failure.

### Verification
- `ruff check`, `ruff format --check`, `mypy src/covenantiq`, `lint-imports` (13 kept, 0 broken) and `pytest tests/unit` all pass.

## M2 — Docker infrastructure

### Compose
- `docker-compose.yml`: postgres (pgvector pg16), redis, rabbitmq, migrate, api, worker-ingest, worker-analysis, streamlit; health checks, named volumes, `depends_on` ordering as in spec §32.5.
  - Required secrets use `${VAR:?message}`, so a missing password fails immediately.
  - `CIQ_OFFLINE` is passed through to every backend container.
- `docker-compose.override.yml` (dev): published ports (5432, 6379, 5672, 15672, 8000), `./src` bind mount, `uvicorn --reload`, Streamlit `runOnSave`.
- `docker-compose.prod.yml`: skeleton only (restart always, log rotation, no build, no streamlit host port). Secrets and the proxy profile are deferred to M19.

### Images and config
- `docker/backend.Dockerfile`: multi-stage (base, builder, test, runtime), uv, non-root user, `/app` path kept identical across stages so the editable install works with the dev bind mount. No ENTRYPOINT or CMD.
- `docker/frontend.Dockerfile` and a placeholder Streamlit app in `frontend/`.
- `docker/postgres/init/00-extensions.sql` (`vector`), `docker/redis/redis.conf` (256mb, `allkeys-lru`, no persistence), `docker/rabbitmq/rabbitmq.conf` (2h `consumer_timeout`, watermarks) and `definitions.json` (vhost `covenantiq`).
- `.dockerignore` updated per spec §32.4. `Makefile` gained `up`, `down`, `logs`, `ps`.

### Python
- `api/main.py` (`create_app()`) and `api/routers/health.py` (`GET /api/v1/health/live`).
- `workers/celery_app.py` (`create_celery_app()`): RabbitMQ broker, Redis result backend, JSON serialization, `acks_late`, `reject_on_worker_lost`, prefetch 1, 24h result expiry. No tasks yet.
- `covenantiq migrate` stub (no-op until M3).
- Settings: blank API keys (as passed by Compose) now count as unset.
- New dependencies: celery[redis], fastapi, uvicorn[standard]; dev: httpx.

### Tests
- 41 passing, 3 skipped, 97.8% coverage. New: health endpoint, Celery configuration, blank-key handling, `migrate`.
- `tests/e2e/test_infra_smoke.py` (API live, RabbitMQ connection, Redis ping) runs only with `CIQ_E2E=1` against a running stack.

### Verified without Docker
- All three Compose files parse; the `depends_on` graph and `definitions.json` are consistent.
- uvicorn serves `/api/v1/health/live` locally; `celery -A covenantiq.workers.celery_app report` finds the app.

### Not yet verified (needs Docker)
- `docker compose up --build` reaching healthy; image builds; RabbitMQ loading `definitions.json` alongside `RABBITMQ_DEFAULT_*`; `psql` showing the `vector` extension; the e2e smoke tests.

## Deviations from the spec

- M2: the backend image has no ENTRYPOINT. The spec says the entrypoint is the `covenantiq` CLI, but its own Compose commands start with `uvicorn` and `celery`, which would break.
- M2: the Docling model-download stage is deferred to M7, when the dependency is added.
- M2: the API health check uses `/health/live` until readiness checks exist.
- Only M1-relevant dependencies were added (pydantic, pydantic-settings, structlog, typer, plus dev tools). The rest are added in the milestone that uses them.
- `enums.py` holds only `Environment` and `LogFormat`; status enums arrive with the ORM models in M3.

## Open items and next steps

- Install Docker Desktop (not present on this machine), then run `cp .env.example .env`, `make up`, `make ps` and `CIQ_E2E=1 uv run pytest tests/e2e` to close out M2.
- M1 and M2 are committed locally on `main`; nothing has been pushed.
- Next: M3 — PostgreSQL and SQLAlchemy (models, Alembic, repositories, unit of work).
