# CovenantIQ — Progress

Source of truth: [PROJECT_SPEC.md](PROJECT_SPEC.md). Milestones are defined in §36.

## Status

| Milestone | Status |
|---|---|
| M1 — Repository setup and architecture skeleton | Done and committed |
| M2 — Docker infrastructure | Code complete and committed; Docker run not yet verified (Docker not installed) |
| M3 — PostgreSQL and SQLAlchemy | Code and tests complete and committed; pgvector-dependent tests unverified (need Docker or a pgvector database) |
| M4–M19 | Not started |

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

## M3 — PostgreSQL and SQLAlchemy

### Built
- `db/base.py` (naming convention, enum mapping, mixins), `db/scope.py` (`OwnershipScope`), `db/session.py` (pooled and NullPool engines), `db/uow.py` (`UnitOfWork`), `db/migrations.py` (head and current revision helpers).
- 15 ORM models in `db/models/` covering every table in spec section 14, with the constraints from section 15.3. All 16 DB enums live in `core/enums.py`.
- 14 repositories in `db/repositories/`. Every user-data method takes a scope; worker-only methods are prefixed `system_`; state changes use compare-and-set (`cas_transition`). `ChunkSearchRepository` is a stub (M9).
- Alembic: `alembic.ini`, async `migrations/env.py` (excludes LangGraph checkpointer tables), migrations 0001 to 0005.
- `covenantiq migrate` now runs `alembic upgrade head`. `GET /api/v1/health/ready` checks the database and that the schema is at head; the API health check in Compose now uses it. The API lifespan owns the DB engine.

### Tests
- Unit (no database): scope-contract reflection (every user-data repository method takes `scope` or is `system_*`; only users are exempt; the registry is complete), model metadata, readiness endpoint mapping. 76 unit tests pass.
- Integration (need `CIQ_TEST_DATABASE_URL`; skipped otherwise): constraints, document, vendor, chat, analysis, assessment, citation, clause and chunk repositories, and readiness. Full run: 116 passed, 7 skipped, 97.7% coverage.

### Verified
- Migrations generate offline and apply to real PostgreSQL 18. Downgrade to base leaves 0 tables, 0 enums and 0 extensions; a second upgrade restores 16 tables and 16 enums.
- Acceptance criteria: duplicate SHA rejected only for active documents; citation CHECK rejects zero or two parents; one active job per document; message sequence and client-id uniqueness; concurrent compare-and-set has exactly one winner (documents, processing jobs); concurrent identical analysis requests create one job; ownership isolation on every repository; uncommitted work discarded; reflection test passes.
- ORM models compared against the migrated database: no differences except the two pgvector stand-ins used on the test machine.
- Lint, mypy (strict) and the 13 import contracts pass.

### Not yet verified (needs pgvector)
- The test machine has no Docker, the installed PostgreSQL 18 has no pgvector, and the only embeddable build I could use lacks citext and pg_trgm. I ran the tests on a private throwaway PostgreSQL 18 cluster with `REAL[]` substituted for `VECTOR(1024)` and the HNSW index skipped.
- Seven tests skip: three Docker smoke tests, two migration tests (round trip through the real migrations, models versus migrated schema) and one embedding-write test. Please run `CIQ_TEST_DATABASE_URL=... uv run pytest tests` once against `pgvector/pgvector:pg16`; that is the check that closes M3.
- Embedding writes through asyncpg may need pgvector's asyncpg codec registration. Decide when M8 exercises it.

### Fixed along the way
- A unit test for `migrate` was silently running a real migration against whichever database the environment pointed at. It now mocks the Alembic call.

## Deviations from the spec

- M3: citations are created in migration 0005, not 0004, because they reference findings and risk dimensions.
- M3: `document_processing_jobs` has a `created_at` column (not in the spec's ER diagram), needed for the stuck-job sweep.
- M2: the backend image has no ENTRYPOINT. The spec says the entrypoint is the `covenantiq` CLI, but its own Compose commands start with `uvicorn` and `celery`, which would break.
- M2: the Docling model-download stage is deferred to M7, when the dependency is added.
- M2: the API health check uses `/health/live` until readiness checks exist.
- Only M1-relevant dependencies were added (pydantic, pydantic-settings, structlog, typer, plus dev tools). The rest are added in the milestone that uses them.
- `enums.py` holds only `Environment` and `LogFormat`; status enums arrive with the ORM models in M3.

## Open items and next steps

- Install Docker Desktop (not present on this machine), then run `cp .env.example .env`, `make up`, `make ps` and `CIQ_E2E=1 uv run pytest tests/e2e` to close out M2.
- M1 and M2 are committed locally on `main`; nothing has been pushed.
- Run the pgvector verification described under M3 once Docker is available.
- Next: M4 — Authentication (Argon2 hashing, JWT, register/login/me, rate limiting).
