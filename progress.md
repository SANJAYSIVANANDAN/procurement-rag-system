# CovenantIQ — Progress

Source of truth: [PROJECT_SPEC.md](PROJECT_SPEC.md). Milestones are defined in §36.

## Status

| Milestone | Status |
|---|---|
| M1 — Repository setup and architecture skeleton | Done and committed |
| M2 — Docker infrastructure | Committed, with fixes made after the first real run (RabbitMQ bootstrap, see "Docker verification"); stack builds and is healthy. The M2 fixes were committed in `7d8411e` |
| M3 — PostgreSQL and SQLAlchemy | Done and committed; the pgvector tests now pass on a real pgvector database |
| M4 — Authentication | Done and committed; Redis rate limiting and RabbitMQ readiness verified against the real stack |
| M5 — Document management | Code and tests complete, committed in `7d8411e`; libmagic verified on Windows and in the Linux image (via the e2e upload) |
| M6 — Celery and RabbitMQ | Code and tests complete, committed in `7d8411e`; real-broker run found and fixed two bugs (DLQ commands, lost-worker jobs); dead-lettering, replay and the worker-kill drill verified |
| M7 — Document extraction | Code and tests complete, committed in `7d8411e`; verified with real Docling on Windows and on Linux (inside the image) |
| M8 — Chunking and embeddings | Code and tests complete, committed in `7d8411e`; vector writes verified on real pgvector; the real Voyage API is unverified |
| M8+ — OpenAI embeddings, Ettin and Cohere rerankers | Code and tests complete, committed in `7d8411e`; none verified against the real hosted APIs (no keys); Ettin reranker verified in its container |
| M9 — pgvector and hybrid retrieval | Code and tests complete, committed in `7d8411e`; verified on real pgvector with fake embeddings and a fake reranker; never run with real embeddings, a real reranker or real contracts |
| M10 — Redis session memory | Code and tests complete, committed in `22f2e8a`; verified against real Redis and PostgreSQL; the model that writes summaries is a fake (the real adapter is M11) |
| M11 — LangGraph chat orchestration | Code and tests complete, committed in `72d8c56`; verified against real PostgreSQL and Redis with a fake model and a scripted retriever; the real Anthropic API has never been called |
| M12 — Contract analysis | Code and tests complete, committed in `72d8c56`; verified on real PostgreSQL (including a real checkpoint resume) with a fake model and a scripted retriever; the real model has never been asked to assess a clause |
| M13 — Vendor assessment and comparison | Code and tests complete, committed in `72d8c56`; verified on real PostgreSQL (including a real checkpoint resume and the bounded dependency wait) with a fake model; the real model has never read a signal, explained a dimension or written a comparison |
| M14 — RAGAS evaluation | Code and tests complete, committed in `e63ecfe`; the offline regression gate runs for real (real pipeline, fake providers, scripted model) with a baseline file (`eval/baselines/offline_v1.0.0.json`); the full run with real providers and RAGAS's LLM metrics is built but has never been run (no keys) |
| M15 — SSE streaming | Code and tests complete, committed in `e111890`; verified on real PostgreSQL and Redis and over real HTTP with `curl -N` (fake model); never streamed a real model's reply or through a proxy |
| M16 — Streamlit UI | Code and tests complete, committed in `6f12c09`; all nine pages render against the real API (empty account) and against fakes with data; no real browser, no upload through the UI, no real model |
| M17 — Integration testing | Tests and two fixes complete, committed with this milestone; the whole-stack flow test is not written; no CI evidence of flakiness or duration |
| M18–M19 | Not started |

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
- Superseded: all of this was run later; see "Docker verification (after M8)". It found that RabbitMQ started with no user.

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
- Superseded: the pgvector tests were run on a real `pgvector/pgvector:pg16` database (migration round trip, models versus schema, embedding write, and the M8 embedding stage) and pass. See "Docker verification (after M8)". The asyncpg codec question is answered: writes through the ORM `Vector` type work without extra registration.

### Fixed along the way
- A unit test for `migrate` was silently running a real migration against whichever database the environment pointed at. It now mocks the Alembic call.

## M4 — Authentication

### Built
- `core/security.py`: Argon2id hashing via pwdlib (defaults match the spec: 64 MiB, time 3, 4 lanes; outdated hashes are upgraded on login), password policy (12 to 128 characters, bundled common-password list, no repeated single character, not the email), JWT helpers (HS256; claims `sub`, `iat`, `exp`, `jti`, `typ`; 30 s leeway; expiry has its own error, every other failure is one opaque error).
- `services/auth_service.py`: `register` (email normalized, duplicate detected before and after insert), `login` (unknown email still pays for a password check, so timing and errors are uniform), `authenticate` (token to `Principal`, re-checks the user is still active), `get_me`. `services/dto.py` has `Principal` (with `scope()`), `UserDTO`, `TokenDTO`.
- `services/rate_limiter.py`: fixed-window counters in Redis (INCR and EXPIRE in one MULTI), buckets and defaults from spec 30.10, `CIQ_RATE_LIMITS` overrides, `Retry-After` from the time left in the window, fails open (logged) if Redis is down.
- `api/`: `container.py` (engine, session factory, Redis, rate limiter, auth service built once at startup), `middleware.py` (request id echoed or generated and bound to logs, security headers, one completion log line per request), `errors.py` (every error in the spec 16.1 shape; validation errors never echo the submitted value; unexpected errors are opaque), `deps.py` (`get_current_principal`, `rate_limit(bucket)`), `schemas/`, `routers/auth.py`.
- Endpoints: `POST /auth/register`, `POST /auth/login` (JSON), `POST /auth/token` (OAuth2 form, powers the Authorize button in `/docs`), `GET /auth/me`.
- `GET /health/ready` now checks PostgreSQL, the migration head, Redis and RabbitMQ, reporting each separately.

### Verified
- 222 tests pass, 7 skip (3 Docker smoke tests plus the 3 pgvector tests from M3, and 1 new smoke test). Coverage 96.9%. Lint, mypy (strict) and the 13 import contracts pass.
- Acceptance criteria: expired token gives 401 `TOKEN_EXPIRED`; invalid, missing, tampered, wrong-secret, wrong-type, deleted-user and inactive-user tokens give 401 `UNAUTHENTICATED`; the 6th login per minute for one email gives 429 with `Retry-After`; every response (including errors) carries `X-Request-ID`; passwords and tokens never appear in logs.
- Also covered: wrong password and unknown email give identical responses; concurrent duplicate registrations give one 201 and the rest 409; the eleventh login per minute from one IP gives 429; email addresses never appear in Redis keys; the real app (uvicorn, production Argon2 settings) registered, logged in and served `/auth/me` against PostgreSQL.

### Not yet verified
- (Verified later against real Redis; see "Docker verification".) The rate limiter first ran against an in-memory stand-in for Redis (no Docker). The MULTI/EXPIRE behaviour and Redis 7 semantics are untested. `tests/e2e` has the readiness smoke test for when the stack runs.
- (Verified later: the readiness endpoint reports rabbitmq `ok` against the real broker.) The probe was first exercised only with the connection stubbed (and against an unreachable port, which correctly reports `unavailable`).

## M5 — Document management

### Built
- `ingestion/validation.py` (spec 18.2): extension allow-list (macro formats get their own message), libmagic sniffing of the first 8 KiB with extension and content required to agree, a PDF probe with pypdfium2 (page count and limit, password-protected and owner-password-only PDFs both detected as encrypted, corrupt files), a DOCX probe (zip integrity, required Word parts, macro project and macro content types rejected, zip-bomb guards on total size, per-part compression ratio and part count), and filename sanitising.
- `ingestion/storage.py`: `FileStorage` protocol and `LocalFileStorage` (files at `{owner}/{document}/{sha256}.{ext}`, atomic `os.replace` from a temp file on the same volume, every path checked to stay inside the root).
- `services/document_service.py`: `upload` streams to disk in 1 MiB steps while hashing and sniffing, enforces the size limit while reading, probes the structure, checks for a duplicate, stores the file, inserts the document and a queued job in one transaction, then dispatches. A lost race on the unique index is caught, the loser's file is deleted and the winner is returned. Also `list` (filters, keyset cursor), `get`, `get_status`, `update` (only sent fields), soft `delete` with purge dispatch.
- `services/vendor_service.py`: create, list (substring and pg_trgm typo-tolerant search, keyset cursor), get (with documents), update; names normalized and unique per owner.
- `services/pagination.py` (opaque keyset cursors), `services/task_dispatcher.py` (`NullTaskDispatcher` stub until M6).
- `api/limits.py`: `BodySizeLimitMiddleware` refuses an oversized upload immediately when Content-Length is too large, and for chunked bodies aborts the read as soon as the running total passes the limit.
- Endpoints: `POST /documents/upload` (202, or 200 with `duplicate: true`; `Location` header), `GET /documents`, `GET /documents/{id}`, `PATCH`, `DELETE` (204), `GET /documents/{id}/status`; `POST/GET /vendors`, `GET/PATCH /vendors/{id}`. All behind the per-user `default` rate limit, uploads also behind the `upload` bucket.
- Guards: `TOO_MANY_ACTIVE_JOBS` (429) per user; an unknown or foreign `vendor_id` is a 404 before anything is stored; a dispatch failure marks the job and document `failed` with `DISPATCH_FAILED` and returns 503.

### Verified
- 390 tests pass, 7 skip (same 7 as before). Coverage 95.4%. Lint, mypy (strict) and the 13 import contracts pass.
- Acceptance criteria: 19 invalid fixtures (wrong or fake type, macro formats, encrypted, too many pages, corrupt, empty, zip bomb) each get the right code and leave no row, file or dispatch behind; a 26 MB upload gets 413 (both with a declared length, and chunked, where the server stops reading after about 25 MB); the same file twice returns `duplicate: true`; 8 concurrent identical uploads create exactly one document, one job and one file; user B gets an indistinguishable 404 for user A's documents, status, patch and delete.
- Also covered: keyset pagination walks every row exactly once, tampered cursors give 400, LIKE wildcards are literal, expiry date is checked against the stored effective date, vendor name uniqueness under concurrency, deleting then re-uploading the same bytes works.
- The real app (uvicorn, real storage) accepted PDF and DOCX, flagged the duplicate, rejected an encrypted PDF and an executable renamed `.pdf`, and refused a genuine 26 MB upload with 413 in 0.2 s before any body was sent.

### Not yet verified
- libmagic and pdfium were first exercised on Windows only. The Linux image has since been built and a real PDF upload works through it (e2e); DOCX upload was not repeated in the container.

### Known limitations
- Until the M6 workers exist, every accepted upload stays `queued`, and queued jobs count toward the per-user active-job limit (default 10), so a user can currently upload about 10 distinct documents.
- After a dispatch failure the document is `failed`; uploading the same bytes again returns that failed document as a duplicate. M6 should add re-dispatch for failed or stuck documents.

## M6 — Celery and RabbitMQ

### Built
- `workers/queues.py` (spec 25.2, 25.6): one durable direct exchange `covenantiq`; four work queues (`document_processing`, `embedding_generation`, `contract_analysis`, `vendor_analysis`), each declared with `x-dead-letter-exchange` and `x-dead-letter-routing-key` arguments; a `covenantiq.dlx` exchange with a durable `<queue>.dlq` for each; routes for all six tasks; `declare_topology` (idempotent, bounded retries), called by workers on `worker_ready` and by the API at startup so the DLQs exist before the first failure.
- `workers/app_factory.py` and `celery_app.py`: full spec 25.1 configuration (late acks, reject on worker loss, prefetch 1, started tracking, 24 h results, no auto-created queues). The factory has no import-time side effects (so `covenantiq --help` needs no environment); `celery_app.py` is the object workers load. API and CLI use a publisher-only app with no result backend.
- `workers/base_task.py` (`CovenantTask`): binds `task_id`, `job_id` and `request_id` to every log line; retries transient failures (including database connection errors) with capped exponential backoff and jitter; a timeout is retried once; any final failure marks the job `failed` in PostgreSQL first and then raises `Reject(requeue=False)`, which is what dead-letters the message. A failure to record the failure still dead-letters.
- `workers/runtime.py`: one event loop and one container per worker child (NullPool engine); `run_async` cancels and unwinds an interrupted coroutine so a soft time limit cannot leave half-finished work for the next task. `workers/heartbeat.py`: a 30 s heartbeat context manager for long steps. `workers/signals.py`: JSON logging with redaction in workers, process init and shutdown, topology declaration.
- Tasks: `ingest.process_document` (stub that walks the job queued, running, succeeded and the document to `ready`; M7 replaces the body), `ingest.purge_document` (real: hard-deletes a soft-deleted document's clauses, chunks and file; never touches a live document), and stubs for `ingest.embed_document` and the three analysis tasks that fail permanently with `NOT_IMPLEMENTED`. Time limits, retries and `rate_limit="30/m"` as in spec 25.3.
- `services/job_service.py`: claim (queued to running for exactly one caller, or takeover of a running job whose heartbeat is over 10 minutes old), heartbeat with stage and progress, success, failure (also fails the document), replay reset (failed to queued), stuck-job listing, and the user-facing lookup. `GET /api/v1/jobs/{id}` serves processing and analysis jobs; others' jobs are a 404.
- `services/task_dispatcher.py`: `CeleryTaskDispatcher` (tasks carry only ids plus the request id; routing comes from the app's routes; fails fast with a bounded retry policy), a cached `QueueDepthProbe`, and `NullTaskDispatcher`. Wired into `DocumentService.upload`, so a publish failure marks the job failed and returns 503.
- Backpressure: uploads get `429 INGESTION_BACKLOG` (with `Retry-After`) when `document_processing` holds more than `CIQ_MAX_INGEST_BACKLOG` messages. The depth probe is cached for 10 s and fails open.
- `services/sweep_service.py`: re-publishes jobs `queued` for over 5 minutes; a Redis lock allows one sweeper at a time and each job is re-published at most once per 10 minutes. It runs as `covenantiq sweep` and as an API background loop (`CIQ_SWEEP_INTERVAL_SECONDS`, default 60, 0 disables).
- `services/dlq_service.py` and CLI: `covenantiq dlq list <queue>` (non-destructive) and `covenantiq dlq replay <queue> [--job-id]` (resets the job by compare-and-set, republishes, and only then removes the message; skips jobs that are not failed, unknown tasks, and publish failures, leaving those messages in the DLQ).
- Fixed a known M5 limitation: uploading the same bytes again after a `DISPATCH_FAILED` now re-schedules that document instead of returning a document that can never finish.
- `docs/runbook.md`: stuck jobs, DLQ replay, the one-time queue reset, the worker-kill drill.

### Verified
- 534 tests pass, 15 skip (the 7 from before plus 8 new gated end-to-end tests). Coverage 94.9%. Lint, mypy (strict) and the 13 import contracts pass.
- Against kombu's in-memory broker (real topology and routes): each task routes to the spec queue with only ids and the request id in the message; persistent delivery on durable queues; the depth probe, DLQ peek (removes nothing), filtered replay, acknowledgement and requeue logic all behave.
- Through Celery's own eager execution: permanent errors, unexpected errors, retried-then-successful tasks (no failure recorded), retry exhaustion, database errors treated as transient, a timeout retried once, a failing `mark_failed` still dead-lettering; the state is `REJECTED` each time a message would be dead-lettered.
- Against PostgreSQL: the job claim has exactly one winner among 8 concurrent callers; a stale-heartbeat takeover has exactly one winner and counts as attempt 2; a live worker's job is left alone; finished jobs are never claimed; replay resets exactly once; the real tasks run end to end (upload, process, job `succeeded`, document `ready`, duplicate delivery ignored, redelivery after a dead worker, failure then reset then success, purge removes rows and file).
- The real API (uvicorn) started in about 8 s with RabbitMQ and Redis both down, answered a clean 503 `DISPATCH_FAILED` with the document marked `failed`, kept serving, and logged no secrets.

### Findings worth knowing
- With the broker down, an upload originally took 119 s to fail, because the API's Celery app subscribed to Redis for results on every publish. The API and CLI now use a publisher-only app; it takes about 14 s with both Redis and RabbitMQ unreachable on this Windows machine, which is slow to refuse loopback connections (Linux refuses instantly). Not yet measured on Linux.
- One run of the full suite showed a single failure and error that I could not reproduce (three isolated reruns and two more full runs were clean). That run was also unusually slow (7.5 minutes against about 5).

### Not yet verified (needs the Compose stack)
- Superseded: all of it ran against the real stack (dead-lettering, replay, redelivery after a killed worker, results in Redis db 1, readiness probe, Redis sweep lock and rate limiter). Celery prefork workers now run (in Linux containers). See "Docker verification (after M8)" for the two bugs this found.
- `consumer_timeout` (2 h) and the passive queue-depth declare under a real backlog were not exercised.

## M7 — Document extraction

### Built
- `ingestion/extraction/types.py` (`ExtractedBlock`, `ExtractionQuality`, `ExtractionResult`) and `base.py` (the `DocumentExtractor` protocol).
- `ingestion/extraction/structure.py`: pure logic, no Docling. A heading stack in reading order; clause references parsed from numbering (`12`, `12.3`, `Schedule 2`, `Article IV`, `Section 4.2`, and sub-clauses made relative as `12.3(a)`); a number decides a heading's depth over the extractor's own level; a bare number or a year in prose is never a reference; headers and footers dropped; the document title kept out of heading paths; tables exported as markdown and split in groups of at most 60 rows with the header repeated (counted once).
- `ingestion/extraction/quality.py`: pages with text, characters, tables, headings, pages needing OCR, and the gates (fewer than 80 percent of pages with text, or under 200 characters per page, marks `insufficient_text`; no text at all fails with `NO_EXTRACTABLE_TEXT`).
- `ingestion/injection_scan.py` (spec 18.7): weighted features combined as independent evidence (instructions aimed at an AI, role tags, long base64 blobs, hidden characters, second-person density). Invisible characters are stripped from stored text but still reported. A score of 0.5 or more flags a block.
- `ingestion/extraction/docling_extractor.py`: one converter per process (OCR off, accurate tables), imported lazily so the API never loads Docling or PyTorch; page-batched conversion for PDFs over 50 pages with progress after each batch; list numbering that Docling strips into a separate marker is put back (so `1.1` survives); failures become `EXTRACTION_FAILED`. Models load once per worker child at start-up.
- `services/ingestion_service.py`: `process_document` now claims the job, extracts in a thread with a heartbeat and progress, applies the text gate, stores `extraction_quality` (and `injection_warning` and flags in the document's metadata), moves the document to `chunking` and closes the job. `GET /documents/{id}` also returns `metadata`.
- `Dockerfile`: a `models` stage downloads only `layout` and `tableformer` into `/opt/docling-models` (`DOCLING_ARTIFACTS_PATH`); the worker's start-up allowance is 300 s.
- New fixtures in `tests/fixtures/documents.py` (generated in code): a 10-section MSA as PDF and styled DOCX with a header, footer and fee table, a scanned PDF, and a PDF with an injection attempt.

### Verified
- 651 tests pass in the regular suite (15 skipped, 21 slow deselected); 403 of them need no database. Coverage 93.3% (the Docling adapter itself is exercised only by the slow tests). Lint, mypy (strict) and the 13 import contracts pass.
- Slow suite, real Docling (`uv run pytest -m slow`): 21 pass. Every one of the 10 numbered sections has the right heading path, clause reference and page; numbered sub-clauses keep their numbers; the page of a known paragraph is correct; the fee table becomes a markdown table under its section; headers and footers are gone; the scanned PDF fails the gate with `NO_EXTRACTABLE_TEXT`; the injection clause is flagged and its neighbours are not; page batches merge into the same result as one pass; corrupt PDF and DOCX fail with `EXTRACTION_FAILED`; end to end (upload, task, API) a real MSA ends `chunking` with its quality stored, a scan ends `failed` with the right code, an injection is flagged on the document, a DOCX has no page numbers.
- Scan tuning: the spec's negative cases ("the Customer shall ignore ...", "respond with a written notice", "system prompt maintenance windows") all stay under 0.3.

### Findings worth knowing
- **M5 bug found and fixed:** libmagic classified a genuine python-docx file as `application/octet-stream` (even given the whole file), so real Word files would have been refused at upload. The earlier tests used a tiny hand-built DOCX that libmagic did recognise. The sniff for `.docx` now also accepts an unclassified file that starts with the ZIP signature; the structure probe still proves it is a real, macro-free DOCX. Regression tests use the realistic file at unit and API level.
- Docling strips list numbering from the text (`1.1` becomes a marker); without re-attaching it, every clause reference below the section level would be lost.
- Cold imports and model loading are slow on this machine: the first `import docling` took about 7 minutes, and loading the PDF models between 1.5 and 20 minutes (conversion afterwards takes seconds). Hence the lazy import, the per-process converter, the warm-up at worker start, and one shared converter in the slow tests.
- Docling placed one fixture's title last in reading order (a layout-model quirk); it does not affect clause references or paths.
- Docling pulled in PyTorch and downgraded `typer` (0.29 to 0.27); the CLI still works. CI installs will be much heavier now.

### Known limitations
- DOCX has no page numbers, so DOCX blocks and citations carry none (`page` is null).
- Extraction results are not stored yet; chunking and embedding (M8) will build on the same task. Until then a processed document stays at `chunking` and its job is marked succeeded.
- A soft time limit cannot stop the extraction thread (Python cannot kill threads); the task fails and is dead-lettered, but the thread runs to completion.

### Not yet verified
- (Verified later: the image builds with the models baked in and Docling runs on Linux in the worker; see "Docker verification".) Still unverified: the 21-test slow suite on Linux, and DOCX extraction in the container.
- The slow suite is not part of the default run; CI needs a job with the models cached to run it.

## M8 — Chunking and embeddings

### Built
- **Clause building** (`ingestion/chunking/clause_builder.py`): blocks are grouped under headings; the heading depth (1 to 3) is the deepest whose clauses have a median of at least 80 tokens, or `CIQ_CLAUSE_HEADING_DEPTH`. A clause over 1500 tokens is split at block boundaries into `5`, `5#2`, ... Each clause gets clause-type labels from `config/clause_taxonomy.yaml` (`ingestion/taxonomy.py`, whole-word phrase matching on the heading and the first 300 characters, multi-label).
- **Sentence splitting** (`sentence_splitter.py`): returns character spans, so every chunk is an exact substring of its clause. Knows legal abbreviations (`Sec.`, `No.`, `i.e.`, `Ltd.`), initials, numbered labels, and splits a run-on sentence at `;`, then `,`, then a space.
- **Chunker** (`chunker.py`): about 350 tokens per chunk, at most 500, one sentence of overlap (if it is 60 tokens or less); clauses under 40 tokens stay whole; a table is its own chunk, or row groups with the header repeated when over 500 tokens. Each chunk keeps its pages and the highest injection score of the blocks it covers. Token counts are estimated as characters divided by 4.
- **Context header** `Title | heading path | clause ref` is stored in `metadata.heading_text` (full-text weight A) and put in front of the passage only when embedding.
- **Embeddings** (`ai/embeddings/`): provider protocol with `embed_documents` and `embed_query`; Voyage (`voyage-law-2`, 1024-d, `input_type` document or query), an OpenAI-compatible adapter, a deterministic hash provider for tests and offline mode, and a retry wrapper (exponential backoff with jitter, transient errors only). Error kinds: unavailable (retry), rejected input, auth failed.
- **Pipeline** (`services/ingestion_service.py`): `process` extracts, chunks, stores clauses and chunks in one transaction (replacing any earlier ones), then hands the job to the embedding stage and publishes `ingest.embed_document`. The embedding stage loops over chunks without vectors in batches of `CIQ_EMBEDDING_BATCH_SIZE`, commits each batch, halves a rejected batch to isolate a refused passage, and finishes with the document `ready` (at most 2 percent refused) or the job failed (`EMBEDDING_FAILED`, `EMBEDDING_AUTH_FAILED`). A retry sends only what is missing.
- **Reliability fixes**: `JobService.hand_off`, `JobService.release`; `CovenantTask` releases the job before each retry; the sweep is stage-aware (an idle job at `embedding` is re-published as an embedding task) and counts idleness from the last heartbeat.
- **`covenantiq reembed [--document-id]`** (`services/reembed_service.py`): clears vectors of ready or failed documents and queues a new embedding-stage job; nothing is extracted or chunked again.
- Migration 0006 (page columns nullable), `docs/adr/ADR-012.md` addendum, `docs/runbook.md` "Embedding" section.

### Verified
- Lint, mypy (strict, 129 files) and the 13 import contracts pass.
- New tests: sentence splitter (including a property test that spans are ordered and cover all text), taxonomy, clause builder, chunker (including a property test that chunks are exact substrings, cover the clause and stay under the limit), embedding providers (hash, retry, OpenAI-compatible over a mock transport, factory), chunk persistence, embedding stage (batching, resume after a failure, refused passages, auth failure, missing provider, deleted document, repeated runs), sweep and retry release, `reembed`, repository additions, CLI.
- Full run (private PostgreSQL, coverage): **780 passed, 15 skipped, 21 slow deselected, 0 failed**; coverage 93%. The final run on real pgvector, after the Docker fixes, is under "Docker verification" (790 passed). An earlier run, before the fixes listed under Issues, had 5 failures. The run was slow (34 minutes) because the machine was under load; that is also when the vendor-name test timed out once.

### Issues found and fixed during M8
- **Retries never worked (M6 and M7 too).** A retried task found its job still `running` with a fresh heartbeat, was told "not claimable" and silently ended, leaving the job `running`. Fixed by releasing the job before each retry.
- Four worker-flow tests still assumed the job succeeds at `chunking` and that `embed_document` is a stub; rewritten for the two-stage flow (the permanent-failure test now uses a scan with no text).
- Test fixtures: the API test container lacked `reembed_service` (164 setup errors); two embedding factory tests were sensitive to a leaked `CIQ_OFFLINE`.

### Known issues and limitations
- `test_constraints.py::test_vendor_names_are_unique_per_owner` timed out once in the full run (13 minutes in, machine under load) and passes alone. Same kind of rare flake as in M6; not reproduced.
- Token counts are an estimate (4 characters per token); real Voyage token counts will differ a little, which only matters near the 500-token limit.
- Clause-type labels are keyword hints, not classification. A clause with unusual wording gets no label; the analysis stage must not depend on labels alone.
- The Redis provider-throttle from the spec is not used (see deviations); under a very large backlog the provider's 429 responses are handled by backoff only.
- A soft time limit still cannot stop the extraction thread (from M7).

### Not yet verified
- ~~Writing real vectors to a `vector(1024)` column~~ Verified later on real pgvector (see "Docker verification").
- The real Voyage API (response shape, rate limits, `input_type` behaviour); only fakes and a mock transport were exercised.
- The HNSW index is untested with data (M9 retrieval will exercise it).
- Chunk quality on real contracts: tested on generated fixtures, not a set of real agreements. Worth checking chunk sizes and clause boundaries on 3 to 5 real contracts before M10.

## OpenAI embeddings support (after M8)

- `text-embedding-3-small` (and `-large`) work through the OpenAI-compatible adapter. They return 1536 (3072) dimensions natively, so the adapter sends `dimensions=1024` to fit the `vector(1024)` column, for documents and queries alike; other OpenAI-compatible servers are not sent the field. Settings validation rejects a dimension above the model's native size, and `OPENAI_API_KEY` is accepted as well as `CIQ_OPENAI_COMPAT_API_KEY`. Compose passes `CIQ_EMBEDDING_MODEL`, `CIQ_EMBEDDING_DIM`, `CIQ_OPENAI_COMPAT_BASE_URL` and `OPENAI_API_KEY` through to the containers.
- New `covenantiq check-embeddings`: embeds three generic sentences, reports model, dimension and a related-versus-unrelated similarity check; prints no keys.
- Tests: the request body (with and without `dimensions`), the factory, the dimension rule and the key alias (mock transport; no network). Unit suite, ruff and mypy pass.
- **Not verified against the real OpenAI API** (no key here). Retrieval quality for legal text with a general-purpose model, versus `voyage-law-2`, is also unmeasured; M9 onward should compare them.
- Set `CIQ_RERANK_PROVIDER=none` when there is no Voyage key, otherwise settings validation still demands `VOYAGE_API_KEY` for reranking.

## M9 — pgvector and hybrid retrieval

### Built
- **`db/repositories/chunk_search.py`**: dense search (cosine `<=>`, `hnsw.ef_search` 100, `hnsw.iterative_scan = relaxed_order` when pgvector has it, an exact scan below `CIQ_EXACT_SCAN_THRESHOLD`), lexical search (`websearch_to_tsquery`, `ts_rank_cd` normalisation 32, bound parameters only), and the lookups that fill in chunks, parent clauses and neighbouring chunks. Every query filters on owner, the allowed documents, and the document being ready and not deleted.
- **`rag/`**: `types.py`; `config.py` and `config/rag.yaml` (initial values, flagged for calibration); `query_analysis.py` (rule-based keywords, quoted phrases kept whole); `fusion.py` (RRF, k 60, clause-type boost 1.1); `retrieval.py` (`RetrievalEngine`: embed, dense and lexical in parallel in separate sessions, fuse, rerank within 3 s or fall back, parent expansion, a trace of ids, ranks, scores and timings and never text); `context.py` (grouping by clause, parent text or chunk plus neighbours, token budget, numbered `<source>` blocks, tag and attribute escaping, injection warning); `evidence.py` (the strong, moderate, weak, none gate, with a stricter path when reranking was skipped); `abstention.py` (the template reply); `citations.py` (the deterministic validator); `cache.py` (query-embedding 24 h and rerank-score 1 h caches in Redis); `session.py` and `factory.py` (wiring).
- **`covenantiq search "<question>" --user <email>`**: a developer CLI that prints the keywords, both searches' ranks, the fused and reranked order, timings, the evidence verdict and the context size (`--json` for the trace).
- New dependencies `rapidfuzz` and `snowballstemmer`. `ADR-019` and a runbook section "Searching and tuning retrieval".

### Verified
- **Full suite on real pgvector: 1108 passed, 17 skipped (opt-in e2e), 21 slow deselected, 0 failed**; coverage 92%. Lint, mypy (148 files) and the 13 import contracts pass.
- New tests (about 250): fusion (a hand-computed example, properties), keywords (hostile input, a property test), the search SQL on real pgvector (ordering, exact versus index scan, model filter, stemming, phrases, SQL-injection strings, the `SET LOCAL` settings ending with the transaction), context (budget, parent expansion, escaping), the evidence gate (the spec's table, thresholds from config), the citation validator (a table of about 50 cases: valid, unknown marker, fabricated quote, changed number, unmarked numeric claim, multi-marker forms, status boundaries, abstention, property tests), the Redis cache (timeouts, breaker, batching), and the engine end to end (40 tests).
- **Acceptance criteria:** for 7 fixture questions the expected clause is in the top 3; 3 unanswerable questions give `insufficient` while an answerable one passes the same gate; another user's identical text is never returned by either search, by hydration or by the engine, even when the caller names their document; deleted and not-yet-ready documents are never returned; retrieval falls back cleanly (fused order, reason recorded) when the reranker times out, fails, is absent or is switched off for the request; the citation validator passes its whole table.
- The CLI was run on seeded data: dense 10 candidates, lexical 3, the right clause `dense#1 lexical#1`, strong evidence; 0.9 s with Redis unreachable.

### Found and fixed along the way
- A **dead or unreachable Redis made one search take 16 s** (about 2 s per cache operation, 8 operations). The cache now has a 250 ms timeout, a circuit breaker (3 failures, then skipped for 30 s) and batched lookups (one `MGET` for all 30 rerank scores); 0.9 s after.
- A single quoted word (`"indemnity"`) was removed from the query and never searched; found by a test, fixed.
- Dense search silently returned nothing when chunks were embedded under another model label; correct (vectors of different models are not comparable) but the trace now says so and names `covenantiq reembed`.
- Test-only: two wrong expectations of mine (`liable` and `liability` do not share a stem; the fixture section 6 is on page 4).

### Deviations from the spec
- The Voyage reranker adapter (`ai/rerank/voyage.py`) is not built; Cohere and the local Ettin model are used instead (ADR-017, ADR-018). The factory raises a clear error for `voyage`.
- The citation validator is stricter than §19.12 in three ways (a fabricated quotation always fails the sentence, a changed number fails it, a marker after the full stop is attributed to its sentence); see ADR-019.
- The engine does not import `db.repositories` only for the search repository: it reaches the database through an injected factory of read-only sessions.

### Not yet verified
- **Nothing here has seen real embeddings, a real reranker or a real contract.** The tests use a deterministic hash embedding and a word-overlap reranker, which prove the plumbing and the filters, not retrieval quality. Whether hybrid search beats dense alone or lexical alone on real contracts is unmeasured; that needs a labelled question set (the retrieval evaluation proposed earlier, and milestone 14).
- The evidence and citation thresholds are the spec's initial values. They have not been calibrated, and the rerank-score thresholds depend on which reranker is configured.
- HNSW with a real-sized corpus (the tests stay under the exact-scan threshold, so the index path with `iterative_scan` was exercised in only two small cases).
- The query-embedding and rerank-score caches against a real Redis (tested with a stub and with an unreachable port).
- The `search` CLI inside the Compose containers: the backend image predates the new dependencies (`rapidfuzz`, `snowballstemmer`) and must be rebuilt first.

## M10 — Redis session memory and conversations

### Built
- **`core/redis_keys.py`**: every Redis key in one module (session window and meta, stream, conversation lock, rate limits, sweep, caches). The rate limiter, the sweep and the retrieval caches were moved onto it, and a test fails if a `ciq:` string appears anywhere else.
- **`ai/llm/`**: the `LLMProvider` port (spec 19.15: `stream`, `structured`, `count_tokens`, `LLMRequest`, `ChatMessage`) and a scriptable `FakeLLMProvider`; `ai/prompts/` with a loader and the versioned `conversation_summary.v1.md`. The real Anthropic adapter is M11.
- **`memory/`**: `SessionMemoryProvider` (the Redis window and meta hash with a TTL on both, pipelined writes in a `MULTI`, overflow detection), `Reconstructor` (rebuild from PostgreSQL), `Summarizer` (model-written rolling summary within a token budget), `MemoryManager` (one entry point; background summarising; V2-ready extension providers), `build_memory_manager`.
- **`services/conversation_service.py`**: create, list (keyset cursor), detail, `PATCH` (rename, document scope, archive, vendor), message history with citations, and the submit-side persistence of a user message: a user message plus a pending assistant message in one transaction with sequence numbers under the row lock, document scope resolved, idempotent replay of a repeated `client_message_id`. Also `memory_context`, which checks ownership before touching Redis.
- **API**: `POST/GET /conversations`, `GET/PATCH /conversations/{id}`, `GET /conversations/{id}/messages` (`before_seq` and a `next_cursor`). New repository methods `advance_summary`, `get_reply` and keyset `list`.
- **Docs**: `ADR-020` and a runbook section "Conversation memory (Redis)".

### Verified
- **Full suite: 1239 passed, 17 skipped (opt-in e2e), 21 slow deselected, 0 failed**; coverage 92%. Lint, mypy (160 files) and the 13 import contracts pass.
- About 130 new tests, the memory ones against **real Redis 7 and real PostgreSQL**:
  - **Acceptance, deleting keys:** deleting the Redis keys and calling `load` gives the same window (also after a simulated restart).
  - **Acceptance, `last_seq`:** a `last_seq` mismatch (a reply in PostgreSQL that never reached Redis) triggers a rebuild that includes the missing turn.
  - **Acceptance, overflow:** overflow produces a summary in PostgreSQL first and then Redis; the window then holds only what is newer, fits its budget and always keeps the last exchange.
  - **Acceptance, keys and TTL:** the keys contain the user id, the `ciq:<env>:` prefix and a TTL on both; a read refreshes it.
  - **Damaged entries:** 8 kinds of damaged or outdated entry (corrupt JSON, wrong schema version, missing schema, non-numeric meta, wrong token count, out of order, summary out of step) are rebuilt, not trusted, and healed.
  - **Expiry mid-generation:** a session that expires while a reply is being generated is rebuilt with the full history, not restarted with one exchange.
  - **Failed summaries:** a failed summary drops the session and the next load recovers it once the model is back.
  - **Summary ordering:** summaries run one at a time per conversation and an older one can never overwrite a newer one.
  - **Isolation:** two users with the same conversation id never share a key; another user's conversation is 404 before Redis is read, and no key is created.
  - **Concurrency:** ten concurrent submissions get ten distinct consecutive sequence pairs; six racing duplicates of one `client_message_id` store exactly one message.
  - **Scope and API:** document-scope resolution never includes another user's or an unfinished document, and every conversation endpoint answers 404 for another user's conversation.

### Deviations from the spec
- `MemoryManager.load` and the providers take a `ConversationState` (id, `last_completed_seq`, summary, `summary_through_seq`) instead of the bare conversation id, because the spec's own consistency check needs those values "without an extra query". The service still checks ownership in PostgreSQL first.
- Three extra consistency checks beyond the spec's list (`summary_through_seq` out of step with PostgreSQL, turns out of order, token count that does not add up).
- Pydantic's JSON instead of `orjson`; same behaviour.
- The API container does not build a `MemoryManager` yet: production settings forbid the fake model, and the real adapter is M11 (`build_memory_manager` is ready).
- `POST /conversations/{id}/messages` (submit and start generation) is not an endpoint yet: the persistence is in `ConversationService.submit_message`, the route arrives with the chat graph (M11). The spec's M10 asks only for CRUD plus message listing.

### Not yet verified
- **A real model writing the summaries**: the fake produces a plain digest, so summary quality, cost and latency are unmeasured, and the prompt was never run against a real model.
- **The chat service recording turns**: M11 will call `record_turn` after finalising a reply; until then the memory library is exercised only by tests that simulate turns.
- **Redis under real memory pressure** (eviction with `allkeys-lru`): rebuild is tested by deleting keys, not by Redis evicting them.
- The Compose stack and the backend image (still without the M9 and M10 dependencies and code until it is rebuilt).

## M11 — LangGraph chat orchestration

### Built
- **`ai/llm/`**: the Anthropic adapter (`anthropic_provider.py`: streaming, structured output via `messages.parse`, token counting, typed errors that never carry prompt text or keys, refusal fallback on, `stop_reason` checked, no sampling parameters), `routes.py` + `config/llm_routes.yaml` (effort, token and time budget per route), `retry.py` (2 retries with backoff, only before the first token), `factory.py`, and a scriptable `FakeLLMProvider` with failure injection. New errors `LLMTruncated`, `LLMBadRequest`.
- **`ai/schemas`, `ai/prompts`**: `IntentClassification`, `StandaloneQuery`; `chat_answer.v1.md`, `classify.v1.md`, `rewrite.v1.md`.
- **`streaming/`**: the SSE event models and an order checker, the `StreamPublisher` interface with in-memory and null implementations, and `ConversationLock` (SET NX EX, token, Lua release and extend).
- **`graphs/`**: `GraphDeps` and ports (the graph imports no `db`), `GraphRunner` (event bridge, ordering guarantee, always one terminal event), `node_guard` and safe error mapping, and `chat/` (state, nodes, graph): load_context, classify_intent, respond_direct, rewrite_query, retrieve, assess_evidence, generate_grounded (streams tokens from the graph), validate_citations, abstain, finalize.
- **`services/`**: `ChatService.submit` (ownership, idempotent replay, lock, 409, 503, transaction, producer task, heartbeat, shutdown, stale sweeper) and `PostgresChatStore`. New repository methods `mark_streaming`, `mark_interrupted`; `ConversationService.ensure_owned` and `existing_submission`.
- **API**: `POST /conversations/{id}/messages` (202; 200 on an idempotent replay; the `chat_submit` rate-limit bucket), container wiring (LLM, memory, retrieval engine, runner, chat service), a stale-message sweeper loop in the lifespan, and `covenantiq migrate` creating the LangGraph checkpoint tables.
- **Docs**: `ADR-021`, a runbook section "Chat replies".

### Verified
- **Full suite** (real PostgreSQL 16 + pgvector and Redis 7 in throwaway containers): **1393 passed, 20 skipped (17 opt-in e2e, 3 opt-in live Anthropic), 21 slow deselected, 0 failed**; coverage 90%. Lint, mypy (178 files) and the 13 import contracts pass.
- **Acceptance, idempotency:** the same `client_message_id` returns the same message ids and the fake's call counter does not move; four simultaneous identical submissions store one pair and generate once.
- **Acceptance, concurrency:** a second question while a reply is generating is a `409` and stores nothing; other conversations are not blocked; a stranger gets `404`, never `409`; the lock outlives its own TTL through the heartbeat and is released afterwards.
- **Acceptance, events:** every scripted run, including every failure path, satisfies `message_start → (status|token)* → citation* → one terminal event`.
- **Acceptance, failures:** a model failure before the first token is retried and the user never sees the failed attempt; after a token the message is `failed` with the partial text, no retry, no citations, not remembered; a refusal is not retryable; a retrieval or database failure still ends the stream with one error.
- **Acceptance, outcome:** an answerable question ends `completed`/`grounded` with validated citations in the `citations` table and `last_completed_seq` advanced; an unanswerable one ends `completed`/`abstained` with no model call to answer.
- **Redis down:** `503`, nothing stored. **Shutdown:** a reply still running is marked `interrupted` and its listeners get an `INTERRUPTED` error; one that finishes is untouched. **Sweeper:** only abandoned replies are interrupted. A late failure cannot overwrite a finished reply.
- The Anthropic adapter is tested against the SDK's own HTTP transport with real SSE frames (request shape, streaming, refusal, truncation, structured output, error mapping, no secret or prompt in any error).

### Found and fixed along the way
- **M10 window bug:** rebuilding a conversation's window while a reply was being written included the question being answered, and recording the finished turn added it again (the history showed the question twice). A window now holds only exchanges whose reply completed; failed and interrupted exchanges are not memory. Regression tests added to the memory suite.
- A run that failed before it began would have published `error` as its first event; the runner now opens the stream first so the ordering guarantee always holds.
- A draft answer's trailing whitespace made the stored content differ from the model's text; drafts are trimmed before validation.
- A new test left the LangGraph tables in the shared test database and broke the migrations round-trip test; it now cleans up after itself.

### Deviations from the spec
- One `retrieve` node instead of `retrieve_hybrid` + `rerank` (the engine does both as one operation); `assess_evidence` also builds the context.
- Citation events come from `finalize` after the commit (§26.7), not from `validate_citations` (§22.4's table).
- The production container published to a `NullPublisher` until M15, which added the Redis Streams publisher and the `GET .../stream` consumer.
- `conversation_summary.v1.md` keeps its M10 name instead of `summarize.v1.md`.
- The Anthropic adapter is tested through the SDK's `httpx2` transport, not `respx` (the SDK no longer uses the `httpx` that `respx` patches). The `@live` tests are in `tests/e2e/test_anthropic_live.py`, opt in with `CIQ_E2E_LLM=1`.
- The producer task set is an instance attribute of `ChatService`, not module-level.

### Not yet verified
- **The real Anthropic API.** No key is on this machine: the request shape (effort, the refusal-fallback beta), streaming, structured output and refusal handling are verified against the SDK's behaviour and mocked HTTP, not against the live service. The three opt-in live tests are written and skipped.
- **Answer quality.** The prompts were never run on a real model with real contracts; whether answers cite correctly, whether the validator is too strict or too lax, and what a reply costs and how long it takes are all unmeasured.
- **Real retrieval inside a chat turn.** The integration tests use a scripted retriever (retrieval is covered by its own suite on real pgvector); a full question over an ingested document, end to end, has not been run.
- **The Compose stack and the backend image** still lack the M9 to M11 dependencies and code (`anthropic`, `langgraph`, `psycopg`); the image has to be rebuilt before `migrate` or the API will run in Docker.
- **Reading a reply live:** no SSE endpoint existed at M11; M15 added it.

## M12 — Contract analysis

### Built
- **`config/rules/contract_baseline_v1.yaml`**: the 25 baseline rules of spec 20.2 (queries, keywords, heuristics, extraction fields, questions, severity policy, recommended actions, contradiction value patterns) and five unusual-provision patterns (exclusivity, unilateral amendment, non-solicit, liquidated damages, most-favoured-customer). Version `contract_baseline_v1@1.0.0`.
- **`analysis/contract/`** (pure, no I/O): `rules_schema` and `rules_loader` (fail-fast validation, taxonomy cross-check, applicability by contract type), `heuristics`, `status` (the §20.5 decision, §20.7 severity, confidence and evidence sufficiency), `obligations`, `contradictions`, `findings`, `types`.
- **`ai/schemas/analysis.py`** and prompts `rule_assessment.v1.md`, `obligations.v1.md`, `contradiction_confirm.v1.md`.
- **`graphs/contract_analysis/`**: state (JSON-only), nodes, graph (`Send` fan-out of rule batches, aggregate, obligations, contradictions, unusual provisions, evidence validation, findings, persistence) and `ContractAnalysisRunner` (PostgreSQL checkpointer, resume, throttled progress, cleanup).
- **`services/`**: `ContractAnalysisService` (`start`, `run`, `findings`), `PostgresAnalysisStore` (document and chunk reads, delete-then-insert persistence with citations and job success in one transaction).
- **API and worker**: `POST /documents/{id}/analyze` (202; 200 when one is running or current), `GET /documents/{id}/findings` (latest succeeded by default; `job_id`, `severity`, `status`, `category`), the real `analysis.run_contract_analysis` task, container and worker wiring, new repository methods, an `analysis` section in `config/rag.yaml`.
- **Docs**: `ADR-022` and a runbook section "Contract analysis".

### Verified
- **Full suite** (real PostgreSQL 16 + pgvector and Redis 7 in throwaway containers): **1564 passed, 20 skipped (opt-in e2e and live model), 21 slow deselected, 0 failed**; coverage 89%. Lint, mypy (196 files) and the 13 import contracts pass.
- **The decision logic** has a test for every row and boundary of §20.5: absent (confident model, weak score, either alone, a tagged clause blocks it, poor extraction blocks it, the threshold is configurable), problematic, ambiguous, acceptable (red flags downgrade it), insufficient (low confidence, unsupported explanation, nothing to rely on). A clean heuristic scan never produces `acceptable`.
- **The ruleset:** the shipped file loads; every clause type exists in the taxonomy; 12 heuristic patterns and 4 value patterns are tried on text they must and must not match; nine kinds of invalid file are refused.
- **The graph** (fake model, scripted retriever): every applicable rule gets a finding in batches of four; absence records what was searched; a red-flag pattern forces `problematic`/`critical`; a model explanation the passage does not support is not shown as fact; a failed batch or a retrieval failure degrades only its rules; exactly half failing is tolerated and more than half fails the job with nothing stored; obligations keep only deadlines present in the text; a confirmed contradiction cites both passages and a dismissed one is dropped; unusual provisions are found anywhere in the document.
- **Resume on real PostgreSQL checkpoints:** a worker killed after the batches resumes and no batch is asked of the model again; killed in the middle of the batches, the two finished batches are not rerun; the checkpoints are deleted afterwards.
- **Idempotency and isolation:** a second delivery of a finished job is skipped with no model call and no change; persisting twice leaves one set; a second analyse request while one is queued returns the same job; another user's document and findings are 404; filters narrow the list but not the summary.
- **Through the real worker container** (settings-built model, retrieval engine and checkpointer): a document that is not ready fails its job with `DOCUMENT_NOT_READY`; a model that cannot produce valid output degrades every rule and the job fails `ANALYSIS_DEGRADED` with no findings stored.

### Found and fixed along the way
- **Duplicate analyses:** keying the job on a per-document counter meant a second request while one was active created a second job (found by the test that asked twice). The service now returns the active job first.
- Two rule patterns were wrong and the pattern tests caught them: governing law stopped at "and" ("England" for "England and Wales"), and the non-solicit pattern missed "Neither party shall solicit...".
- psycopg's async mode cannot run on Windows' default event loop; the worker runtime and the test suite now select the selector loop there.

### Deviations from the spec
- `RuleAssessment.extracted_values` is a list of `{name, value}` pairs, not a dict (structured output needs a closed schema).
- The model cannot raise a severity (§20.7): the spec's `RuleAssessment` has no field for it.
- The checkpointer connects per run, not through a shared pool of one to four.
- Obligations are stored with `clause_status=acceptable` (the column is not nullable; they are listed, not rated).
- Explanations are checked with the chat citation validator's overlap and number test on the whole explanation, since analysis text has no `[C#]` markers.
- The `validate_evidence` step is mostly done inside each batch (where the sources are in hand); the node that remains is a final defensive downgrade.

### Not yet verified
- **The real model.** No key is on this machine: whether Claude returns valid `RuleAssessmentBatch` output for four rules at once, whether its explanations pass the overlap check often enough, what an analysis costs and how long it takes are all unmeasured. The prompts have never been run.
- **Real retrieval and real contracts.** The tests use a scripted retriever (retrieval has its own suite); the rules' queries, thresholds (`absent_threshold`) and the heuristic patterns were written from the spec and tested on made-up sentences, not tuned on real agreements. Expect false positives and misses until they are.
- **Analysis through RabbitMQ and the Compose worker**, and killing a real worker mid-analysis (the resume is tested by crashing the run in-process).
- The backend image still needs rebuilding (M9 to M12 dependencies and code).

## M13 — Vendor assessment and comparison

### Built
- **`config/vendor/signals_v1.yaml`** (the 26 signals of spec 21.2 with weights, and for the eight extraction signals their queries and question) and **`scoring_v1.yaml`** (dimension weights, risk values, the three absences that count as risk, thresholds, severity bands, comparison limits). Both validated at start-up, including every contract rule id against the contract ruleset.
- **`analysis/vendor/`** (pure; imports no `ai`, `db` or `api`): `config_schema`, `signals` (contract finding to signal), `aggregation` (across documents, with conflict flags), `scoring` (the §21.5 engine), `comparison` (the matrix, ranking rules, template summary, narrative validation), `types`.
- **`ai/schemas/vendor.py`** (no numeric field but source numbers) and prompts `signal_extraction.v1.md`, `explain_dimension.v1.md`, `comparison_narrative.v1.md`.
- **`graphs/vendor_assessment/`**: load scope, verify the contract analyses, map findings to signals, six `Send` branches reading extraction signals, validate evidence, aggregate, score, explain dimensions (validated, template fallback), persist. **`graphs/vendor_comparison/`**: load, check compatibility, compute, narrative, validate, persist. **`graphs/checkpointed.py`**: the checkpoint, resume and cleanup mechanics, now shared by all three analysis graphs (the contract runner was moved onto it).
- **`services/`**: `VendorAssessmentService` (start with dependencies, run with the bounded wait, latest, history), `VendorComparisonService` (preconditions, run, get), `PostgresVendorStore`.
- **API and worker**: `POST /vendors/{id}/assess`, `GET /vendors/{id}/assessments` and `/latest`, `POST /vendors/compare`, `GET /vendor-comparisons/{id}`; the real `analysis.run_vendor_assessment` (with `retry(countdown=30, max_retries=20)` while dependencies run) and `analysis.run_vendor_comparison` tasks; container and worker wiring; repository additions.
- **Docs**: `ADR-023` and a runbook section "Vendor assessment and comparison".

### Verified
- **Full suite** (real PostgreSQL 16 + pgvector and Redis 7 in throwaway containers): **1750 passed, 20 skipped (17 opt-in e2e, 3 opt-in live model), 21 slow deselected, 0 failed** in 12 min 43 s; coverage 88%. Lint, mypy (219 files) and the 13 import contracts pass.
- **Scoring properties (Hypothesis, 120 examples each):** a favorable signal never raises a dimension's score; a riskier status never lowers it; gaps never change a score and only lower coverage; the order of documents never matters; identical inputs give identical outputs; scores stay between the extremes used; the renormalised weights sum to one. Plus the worked examples of §21.5 (coverage, gaps, the three absence overrides, provisional by completeness and by fewer than four dimensions, band edges).
- **Aggregation and mapping:** the spec's status table; two rules behind one signal; conflicts flagged and scored conservatively; absent only if every document says so.
- **Comparison:** dimensions comparable only if every vendor is assessed with coverage of at least 0.6, with the reason per vendor; a clause against an absence is "Missing in Globex — cannot compare"; a policy absence is compared and labelled; the ranking is suppressed with a stated reason for a provisional vendor or fewer than four comparable dimensions; vendor order does not change the result.
- **The model never writes numbers:** the output schemas are inspected for numeric fields; a model explanation that names an unknown signal or states an uncomputed number is replaced by the template; a comparison summary that compares a not-comparable dimension, invents an id, or ranks without a ranking is replaced by the template and the job still succeeds.
- **Dependencies:** starting an assessment starts the missing contract analyses and queues behind them; a still-running dependency puts the job back to `queued` (worker slot freed); a failed one lets the assessment proceed with insufficient-evidence signals and a note; after twenty waits the job fails `DEPENDENCY_NOT_READY` (played out through the real Celery task in eager mode).
- **Storage:** six dimensions and their citations in one transaction with the `is_latest` flip (exactly one latest, history kept); storing a job's assessment twice leaves one; a succeeded job is not run again.
- **Comparison preconditions** (each a 409 naming the vendors): no assessment, different or outdated configuration, a stale assessment (waivable with `require_fresh=false`; also stale after a newer contract analysis); duplicates and counts are 400/422; other users' vendors and comparisons are 404.
- **Resume on real PostgreSQL checkpoints:** a worker killed after the extraction resumes and reads no signal again.

### Found and fixed along the way
- A false positive in the narrative check: "behind" in "the evidence behind each dimension" counted as a comparison. The comparative-word list now uses "falls behind", "ahead of" and similar phrases.
- A comparison test built on a premise that was not true (a vendor with no compliance signals is still complete enough to rank, so a ranking exists); the scenario was changed to a genuinely provisional vendor.
- Test documents without a contract type only receive the 15 rules for all contracts and MSAs, so most vendor signals had no finding; this is realistic behaviour and is why the integration tests use SaaS contracts. In real use, setting the contract type on upload matters.

### Deviations from the spec
- Severity bands are exclusive upper bounds in YAML; scores of `None` get severity `info`; `conflicting` is a flag on a signal rather than a gap reason (the spec's algorithm scores conflicts).
- The narrative length check is 100 to 350 words (spec: 150 to 300).
- `GET /vendors/{id}` does not yet include the latest assessment summary; the assessment history returns summaries without evidence.
- Six `Send` branches as specified; the legal branch has no extraction signals and returns at once.
- A quirk of the specified numbers is recorded in ADR-023: aggregation ranks any unfavorable above ambiguous, but unfavorable-low (30) is less risky than ambiguous (50) in the configured values.

### Not yet verified
- **The real model.** No key is on this machine: whether Claude reads the eight extraction signals correctly, how often its rationales pass the support check, whether its dimension explanations pass the no-new-numbers check, and whether its comparison summaries pass validation or usually fall back to the template, are all unmeasured. Nothing here has run with a real model.
- **Real contracts.** The signal queries, the weights and the thresholds come from the spec and were tested on made-up sentences; the first real vendor will show which signals are noisy.
- **Through RabbitMQ and the Compose workers**, including the 30-second dependency wait across real deliveries and a worker killed mid-assessment.
- **Test suite time:** the integration suite has grown past ten minutes because every vendor test runs full analyses; sharing a seeded, assessed vendor between tests would cut it down.
- The backend image still needs rebuilding (M9 to M13 dependencies and code).

## M14 — RAGAS evaluation

### Built
- **`eval/` package** (at the repository root, covered by ruff and mypy): `dataset.py` (models, loader, manifest checksum enforcement), `datasets/generate_v1.py` (deterministic generator) and the dataset `eval/datasets/v1.0.0/` (12 synthetic contracts as text, 73 chat questions of which 15 are unanswerable, 6 follow-ups and 4 multi-document, complete findings labels for 12 contracts, 5 vendor cases, `manifest.yaml` with per-file SHA-256).
- **Offline pipeline** (`extraction.py`, `scripted.py`, `pipeline.py`, `runner.py`): real ingestion (two-stage `IngestionService`), chunking, hybrid retrieval, evidence gate, chat graph, citation validator, contract analysis graph and vendor scoring, on a throwaway user in real PostgreSQL, with hash embeddings, a lexical reranker and a scripted model (labelled answer when the question has one; a fabricated claim when it does not; rule answers from the labels only if the labelled clause was retrieved). Everything it creates is deleted afterwards.
- **Metrics** (`eval/metrics/`): ratios of (numerator, denominator) per example with a seeded bootstrap 95% interval: retrieval hit@k, MRR, non-LLM context precision and recall, citation correctness and validity, unsupported claim rate, abstention accuracy, false abstention and false answer rates, gate abstention, clause extraction accuracy, risk precision and recall, finding status accuracy, not-expected violations, vendor signal agreement, dimension order claims, scoring determinism.
- **Gate** (`baseline.py`, `config/eval_tolerances.yaml`): compares a run with `eval/baselines/offline_v1.0.0.json` per metric, in the worse direction only (default 0.02, scoring determinism 0.0); missing metrics and runs with errors fail.
- **Reports** (`report.py`): JSON and Markdown with git revision, dataset version and hash, prompt hashes, models per route, ruleset and scoring versions.
- **Full run** (`run_full.py`, `ragas_adapter.py`): real providers (refuses fake ones), asks before spending, the same custom metrics plus RAGAS 0.4.4's six metrics through an adapter that lets RAGAS use our model and embedding providers; prints the spec 28.7 targets beside what was measured. RAGAS lives in an optional `eval` dependency group.
- **Scripts and CI:** `eval/scripts/{run_offline,compare_to_baseline,run_full,build_dataset}.py`; `make eval-dataset | eval-offline | eval-compare | eval-baseline | eval-full`; an `eval-offline` job in `ci.yml` (pgvector service, migrate, run, compare, artifacts, step summary); `.github/workflows/eval.yml` (manual and nightly full run; needs secrets).
- **Small source changes:** `rag/citations.py` gained `count_markers`; the chat graph's `validate_citations` records `markers` and `factual_sentences` in its trace so the metrics need no recomputation. CI's mypy step is now `uv run mypy` (config covers `src/covenantiq` and `eval`).
- **Docs:** `ADR-024`, a runbook section "Evaluation", and `docs/eval/offline_v1.0.0.md` (the measured numbers and how to read them).

### Verified
- **The offline run, on real PostgreSQL 16 + pgvector, about 84 s, deterministic (a second run matched every metric).** Baseline values: hit@1 0.603, hit@3 0.828, hit@5 0.931, MRR 0.732; citation correctness 0.981, validity 1.000; unsupported claim rate 0.019; abstention accuracy 0.795; false abstention 0.155 (n=58); false answer 0.400 (n=15); gate stopped 0.600 of the unanswerable; clause extraction, risk precision and recall, finding status accuracy, vendor agreement and scoring determinism all 1.000. Full table with intervals: `docs/eval/offline_v1.0.0.md`.
- **The perfect scores mean the deterministic logic is right given correct inputs, not that real answers are good:** the scripted oracle supplies the answers.
- **The gate catches real regressions:** returning one passage (`k_final=1`) drops hit@3 from 0.83 to 0.60 and fails; evidence thresholds of 1.0 drop abstention accuracy from 0.795 to 0.205 and fail. Tests: 12 integration tests (about 3.5 min), 85 unit tests on metrics, dataset, baseline and the gate, 18 on the RAGAS adapter and the full-run script (with fakes).
- **Dataset integrity:** changing, deleting or adding a file makes the loader refuse, naming the generator; the generator is deterministic.
- **Lint, mypy (242 files) and the 13 import contracts pass.**
- **Full suite:** **1864 passed, 1 failed, 20 skipped, 21 slow deselected** (real PostgreSQL and Redis in throwaway containers). The run took 8 h 7 min of wall-clock because the machine was suspended part of the time, so it is not a speed measurement. The one failure, `test_session_memory.py::TestRebuildTriggers::test_damaged_or_outdated_entries_are_rebuilt_not_trusted[no_schema-schema_version]`, passed when run alone and when the whole `tests/integration/memory` directory was rerun (38 passed). I could not reproduce it and have not found its cause; a Redis key expiring during the suspension is a guess, not a finding. The next unattended full run will show whether it recurs. Coverage was not measured in this run.

### Found along the way
- The first run disagreed with my labels on `DP-001`: contracts with a personal-data breach clause were labelled "absent", but the system answered `insufficient_evidence` because that clause is tagged data protection and absence may not be claimed while a related clause exists. The system was right; the contracts now have a data protection section.
- **Raising the RRF constant to 1,000 changes no metric offline** (the reranker reorders), so the example regression in the spec's acceptance criteria does not trip this gate. The regressions above do.
- With the fake reranker the evidence gate refuses 15.5% of answerable questions (fee, renewal and follow-up questions with short clauses) and lets 6 of 15 unanswerable ones through to the model; in all six the citation validator marked the fabricated claim `ungrounded` and added its notice, but the answer is still shown. **Decision for you:** should an answer whose only claims are `ungrounded` be replaced by an abstention?

### Deviations from the spec
- Contracts are plain text read by a small `TextExtractor` that feeds the production block builder, not PDF or DOCX through Docling (the offline run must be fast and deterministic). Docling stays covered by the slow suite.
- No CUAD import; all dataset sources are project-owned.
- Not built: `make eval-retrieval` (real embeddings and reranker), vendor extraction stability over three live runs, calibration of the evidence thresholds (task 6), and the committed full report in `docs/eval/`.
- Non-LLM context precision and recall are implemented here (Levenshtein, threshold 0.5) so the offline run needs no RAGAS.
- RAGAS 0.4.4 deprecates `from ragas.metrics import ...`; it must move to `ragas.metrics.collections` before RAGAS 1.0. Adding the `eval` group updated `uv.lock` (jiter and rich moved as a side effect).

### Not yet verified
- **Everything that needs a key:** `run_full.py` and every RAGAS LLM-judged metric have never run; the adapter is tested with fakes only. There is no full report, and the spec 28.7 targets are untested.
- **Retrieval with real embeddings and a real reranker.** The offline hit rates and the gate behaviour depend on the fakes. The evidence thresholds (0.55 / 0.35 / 0.70) are the spec's defaults, not calibrated.
- **The dataset is easy:** short, regular, synthetic contracts. Scores on real agreements will be lower.
- **CI:** the `eval-offline` and `eval.yml` workflows have not run on GitHub (they were checked locally only).
- The backend image still needs rebuilding (M9 to M14 dependencies and code).

## M15 — SSE streaming

### Built
- **`streaming/publisher.py`: `RedisStreamPublisher`** (`XADD ciq:{env}:stream:{message_id} MAXLEN ~ 5000`, fields `event` and `data`). Tokens are merged and written every 30 ms or 64 characters, always before any other event; the key expires 600 s after the terminal event (3,600 s after the first entry if there is none). A Redis error is logged and dropped: the reply still completes in PostgreSQL.
- **`streaming/consumer.py`: `StreamConsumer`** (`XREAD BLOCK` from `Last-Event-ID`, stop at the terminal event, replay of a finished reply from PostgreSQL when its stream is gone, `STREAM_STALLED` after the idle timeout, a reconnect after the terminal event returns nothing) and `replay_frames`.
- **`services/stream_service.py`**: the ownership check (conversation is the caller's, message belongs to it, role is assistant; otherwise 404) and the loader for replays.
- **`GET /api/v1/conversations/{id}/stream?message_id=`** with `sse-starlette` (`ping=15`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Last-Event-ID` header).
- **Wiring:** the container publishes through `RedisStreamPublisher` (was `NullPublisher`) and has a dedicated Redis client for listeners (its socket timeout is longer than the 15 s block). Shutdown already waited 20 s for producers and marked the rest interrupted (M11).
- **Dependencies:** `sse-starlette`, and `httpx-sse` for tests.
- **Docs:** `ADR-025` and a runbook section "Streaming a reply (SSE)".

### Verified
- **Real HTTP, outside the test harness:** uvicorn on the real app (fake model, offline mode), `curl -N` showed `message_start`, `status`, `token` and `message_complete` frames with Redis ids, and reconnecting with the 2nd frame's id returned the other 2 of 4 frames. That run took the abstention path (the smoke database had no documents).
- **Integration tests (real PostgreSQL and Redis, httpx-sse against the ASGI app, 16 tests):** event order and final content; listening before the reply is finished; **reconnecting after every possible frame gives exactly the rest**; reconnecting without an id; the listener leaving does not stop the reply (it completes in PostgreSQL); **replay from PostgreSQL after the stream is deleted** (completed and failed replies, resuming by `0-N` ids, a listener holding a real Redis id); **a producer that died gives `STREAM_STALLED`, then the sweeper marks it interrupted and a later listener gets `INTERRUPTED`**; authorization (other user 404, wrong conversation 404, a user message 404, unknown id 404, anonymous 401, missing `message_id` 422); a Redis failure while publishing leaves the reply completed and served from PostgreSQL; the stream's short expiry; 200 tokens become fewer than 40 writes with the text intact.
- **Unit tests (29):** coalescing by characters and by time, the key expiry, a swallowed Redis failure, and a **Hypothesis property**: text and order survive any pattern of tokens, pauses and status events and the ordering guarantee holds; replay frames for completed, failed and interrupted replies.
- **Lint, mypy (244 files) and the 13 import contracts pass.** Unit, API, chat and memory tests together: 1613 passed.
- **Full suite:** **1892 passed, 0 failed, 20 skipped (17 opt-in e2e, 3 opt-in live model), 21 slow deselected; coverage 94%** (real PostgreSQL and Redis in throwaway containers; 1 h 59 min of wall-clock with coverage on, partly machine suspension, so not a speed measurement). The memory test that failed once in the M14 run passed here, so the earlier failure is still unexplained.

### Found and fixed along the way
- A reconnect after the terminal event (what a browser does when a stream closes) replayed the citations and the terminal event again, because the consumer found the message final and the stream quiet. It now checks whether the stream's last entry is a terminal event the listener already has. The resume test caught it.
- The M2 conftest builds `Container` by hand; it needed the two new fields.

### Deviations from the spec
- The conversation lock is released by `ChatService` when the producer ends, not by the publisher on the terminal event (spec 26.3), because the service must release it on every failure path anyway.
- The consumer is split across `streaming/consumer.py` (Redis) and `services/stream_service.py` (PostgreSQL) because the import rules forbid `streaming` from importing `services`.
- A replayed `message_complete` has empty `timings_ms` (not stored with the message). A merged token's `index` is that of its first token.
- No rate limit on the stream endpoint and no cap on concurrent listeners per user.

### Not yet verified
- **A live stream from a real model.** The stream was exercised with a fake model; the spec's "curl -N shows live events from a real graph run" is true of the real graph but not of a real Anthropic model (no key here).
- **A proxy in front** (nginx or a load balancer): buffering, idle timeouts and the 15 s pings were not tried; `X-Accel-Buffering` is set but unproven.
- **Many listeners and reconnect storms**, and a Redis restart in the middle of a reply (the listener should end with the PostgreSQL replay after the idle timeout; this is tested with a deleted key, not a restart).
- **Streamlit** is the first real browser-side client (M16); the `Last-Event-ID` retry loop of spec 26.9 lives there.
- The backend image still needs rebuilding (M9 to M15 dependencies and code).

## M16 — Streamlit UI

### Built
- **`frontend/`**: `app.py` (navigation, theme CSS, sign-out; signed out there is only the login page), `api_client.py` (typed wrappers for every endpoint the UI uses, `ApiError`/`Unauthorized`/`Unreachable`, SSE stream request), `sse.py` (`stream_events` with up to three reconnects using `Last-Event-ID`, and `ReplyState` which folds events into draft, stages, citations and the validated final text), `presenters.py` (all wording, colours, grouping, exports, the comparison matrix), `state.py` (session, `call` with the 401 and error handling), `navigation.py`, `components/` (severity, citations, finding card, dimension card, common), `.streamlit/config.toml` (theme), and the nine pages in `pages/`.
- **Pages:** Login/Register; Dashboard (KPIs, severity chart, vendor risk overview, recent activity); Document library (filters, row selection, quality warnings, analyze, open chat, edit, delete with confirmation); Upload (multi-file, vendor select or create, per-file progress polled every 2 s); Contract chat (document multiselect, conversation switcher, streamed answers, `[C#]` superscript badges linked to the sources panel, grounding badge, "Why this answer?", Regenerate, input disabled while streaming, earlier messages on demand); Contract risk analysis (job progress, findings by severity and category, status and severity filters, finding cards with evidence, obligations table, CSV and Markdown export, disclaimer); Vendors (list, create, edit, linked documents, assess with dependency message and progress, latest assessment with score bar, completeness, provisional badge, dimension cards, history); Vendor comparison (2 to 5 vendors, precondition messages with "Assess now", matrix with not-comparable cells shaded and explained, chart of comparable dimensions only, signal table, narrative); Conversation history (archived toggle, open, rename, archive).
- **Import contracts:** the UI may not import `covenantiq` and the backend may not import the UI (15 contracts now). mypy covers `frontend/`.
- **Build:** `docker/frontend.Dockerfile` (unchanged, now final), `make ui`, `streamlit`, `altair`, `respx` in the dev group, `frontend/requirements.txt` needs `streamlit>=1.50` (`width="stretch"` replaced the deprecated `use_container_width`).
- **Docs:** `ADR-026` and a runbook section "The web UI (Streamlit)".

### Verified
- **Unit tests (63, `tests/unit/frontend`, no server):** the API client against mocked HTTP with `respx` (token, multipart upload, pagination, error mapping with request id, 401, unreachable, a refused stream); the SSE parser and reconnect (a dropped stream resumes with `Last-Event-ID` and repeats nothing, three reconnects end with a retryable error, an API error is not retried, pings ignored) and `ReplyState`; presenters (markers escaped and linked, grouping and filters, CSV and Markdown, the comparison matrix and chart data excluding not-comparable dimensions); Streamlit `AppTest` runs of the Login, Chat and History pages and of the whole app with a fake client (history with citations and badges, a question asked and streamed with the final text replacing the draft, a failed stream with a partial answer and Regenerate, a 409 explained, an expired token returning to login, the input disabled while streaming).
- **Against the real API (opt-in `tests/e2e/test_ui_flow.py`, uvicorn with the fake model, real PostgreSQL and Redis):** a new account registers and signs in with the real client; all eight pages render without error with the response shapes the API actually returns; a vendor is created through the page; a question is asked, streamed over real HTTP and stored, and the same messages come back from the API. 3 passed.
- **Real servers:** `streamlit run frontend/app.py` starts and answers its health endpoint; the Docker image builds, runs as the non-root user and imports every page.
- **Lint, mypy (268 files) and 15 import contracts pass.**
- **Full suite:** **1955 passed, 0 failed, 23 skipped (17 opt-in e2e, 3 opt-in live model, 3 opt-in UI e2e), 21 slow deselected; coverage 94% of `src/covenantiq`** (the frontend is not in the coverage figure) in 29 min 31 s, real PostgreSQL and Redis in throwaway containers.

### Found and fixed along the way
- `STAGE_LABEL` had `"validating"` twice with different meanings (a job's file check and a chat reply's citation check); the dictionaries are now separate (`stage_label` and `chat_stage_label`).
- Streamlit 1.67 deprecates `use_container_width`; replaced with `width="stretch"` throughout.
- The extraction-quality flags I first assumed did not exist under those names; the document page now reads `insufficient_text`, `ocr_needed_pages`, `injection_flagged_blocks` and the metadata's `injection_warning`.

### Deviations from the spec
- **Dashboard:** there is no endpoint for it, so the totals are computed in the UI from lists (findings of up to 30 ready documents, latest assessments of up to 20 vendors, cached 30 s). "Recent activity" shows the latest document updates, since no endpoint lists jobs.
- **"Why this answer?"** uses what the stream reported (stages, number of citations and documents, grounding, output tokens); the API does not expose a message's retrieval trace, so retrieval scores and the evidence decision cannot be shown.
- **Sign-in does not survive a refresh** (spec 27.3 says the token is re-entered in V1); history reloads after signing in.
- **Document library "view details"** is a panel below the table rather than a drawer. Chat scope is changed with the document multiselect (a PATCH on the conversation), not a separate toggle.

### Not yet verified
- **A real browser.** AppTest runs pages headlessly and the server was only checked for its health endpoint; nothing was clicked in a browser, so layout, the superscript links to citations, the chart rendering, the file uploader and text streaming as seen by a person are unchecked. The Sign out button's click-through could not be driven in AppTest (a rerun leaves an empty tree there); the logout logic is tested directly.
- **Pages with data against the real API.** The opt-in test used an empty new account, so findings, assessment, comparison and document pages were tested with fakes built from the API's schema, not with real analysis output. Uploading needs the worker stack and was not run through the UI.
- **The full acceptance path** (register, upload, chat with citations, analyse, assess two vendors, compare, all through the UI) needs the Compose stack and a real model; it has not been run.
- **Streaming through a real model and a proxy** (see M15).
- The backend image still needs rebuilding (M9 to M15 dependencies and code); the frontend image was built and works.

## M17 — Integration testing

### Built
- **`docs/test-matrix.md`**: every row of the spec's critical list (34.3) mapped to the tests that cover it, the failure-injection cases, and what is not covered. An audit of the existing suite found most rows already covered by M3 to M16; the gaps below were filled.
- **`tests/integration/api/test_ownership_matrix.py`** (11 tests): user B gets 404 on all 17 endpoint cases that take one of A's ids (documents, status, analyze, findings, jobs, conversations, messages, the stream, vendors, assess, assessments), 401 without a token everywhere else, lists show only your own, cross-references are refused or dropped, and **a route that is not classified in the file fails the build** (so a new endpoint cannot skip the check).
- **`tests/unit/graphs/test_chat_injection.py`** (7 tests): a flagged passage reaches the model with a warning, markup in a passage cannot close or open a source block, the prompt tells the model sources are data, and a reply containing the canary is refused.
- **`tests/integration/chat/test_chat_flows.py`** (4 tests, the log tests run in both JSON and console format): Redis flushed in the middle of a conversation (the follow-up still sees the earlier turn, rebuilt from PostgreSQL, and the window returns), and no contract text or question in the logs of a validated reply or a failed one.
- **Contract analysis with a refusing model** (`test_contract_graph.py`): the refused batch's rules degrade, the job succeeds.
- **Code changes the tests forced** (ADR-027): tracebacks no longer print local variables; every answer prompt carries a canary and a reply containing it is replaced by a refusal.

### Verified
- All new tests pass. Full suite: **1978 passed, 0 failed, 23 skipped, 21 slow deselected; coverage 95%** in 29 min 17 s. The run reported 4 errors at setup in `test_chat_flows.py`'s log tests, which were my own doing: I had started pytest with `-p no:logging`, which removes the `caplog` fixture they use. Run normally (`tests/integration/chat/test_chat_flows.py` with `tests/unit/core/test_logging.py`) they all pass (14 tests). The suite should be run without that flag.
- Lint, mypy (268 files) and the 15 import contracts pass.

### Found and fixed along the way
- **Logs could contain contract text, questions and passwords.** structlog renders local variables in tracebacks by default, in the JSON format used in production as well as the console one. The log test caught a conversation turn (with the contract passage) printed that way. Fixed in `core/logging.py`; tested in both formats.
- **No defence against a model that obeys an injected instruction.** The citation check accepts any sentence with a valid marker, so a compliant model's output was marked grounded. Added the canary (see ADR-027). It does not catch an injected claim that the passage itself states.
- Foreign document ids on a conversation are silently dropped (existing design, now tested): the earlier expectation of a 404 was wrong for it.

### Not done
- **`tests/e2e/test_full_flow.py`** (the whole path through a Compose stack): not written, since it cannot be run here (the backend image is stale and the worker stack is not up). The pieces run in `test_worker_flow`, `test_chat_api`, `integration/vendor/` and `e2e/test_ui_flow.py`.
- **Ten consecutive CI runs without a flaky failure, and a suite under ten minutes in CI:** the integration suite is not in CI yet (M18) and takes about 30 minutes here. One memory test failed once in an earlier full run and has not been explained (see M14); no run since has shown it.
- **Tuning thresholds from the evaluation:** the offline numbers come from fake providers (see M14).
- **Log scans cover chat only**, not ingestion, analysis or vendor runs.

## OpenAI as the model provider (after M17)

### Built
- **`ai/llm/openai_provider.py`**: `OpenAIProvider` for OpenAI or any server with an OpenAI-style `/chat/completions`, on `httpx` (no SDK). Streaming with usage, structured output through `response_format: json_schema` (not strict, validated by pydantic: our schemas have optional fields), `max_completion_tokens`, no sampling parameters, `reasoning_effort` from the route's effort (`xhigh` and `max` become `high`; off with `CIQ_OPENAI_SEND_EFFORT=false`), `length` and `content_filter` and a `refusal` field become typed errors, errors carry no prompt text or key. Token counting is an estimate (the API has no such call).
- **Configuration:** `CIQ_LLM_PROVIDER` accepts `openai`; it needs `OPENAI_API_KEY` (or `CIQ_OPENAI_COMPAT_API_KEY`) and uses `CIQ_OPENAI_COMPAT_BASE_URL` (default `https://api.openai.com/v1`), shared with the OpenAI embeddings. The model is `CIQ_LLM_MODEL_DEFAULT`; routes may name others in `config/llm_routes.yaml`. The factory wraps it in the same retry layer as Anthropic.
- **`covenantiq check-llm`**: one tiny streamed and one structured call, prints the model and token counts, never the key.
- **`.env` on this machine** (edited by me without reading any secret): `CIQ_LLM_PROVIDER=openai`, `CIQ_LLM_MODEL_DEFAULT=gpt-5.6-sol`, `CIQ_RERANK_PROVIDER=none`, `CIQ_OPENAI_COMPAT_BASE_URL=https://api.openai.com/v1` added. The settings now load.
- Docs: runbook ("Model configuration") and `.env.example`.

### Verified
- 29 adapter tests (mocked API with `respx`): streaming order and usage, parameter names, effort mapping and switch, truncation, content filter, refusal, every retryable and non-retryable status, no key in any error, the retry wrapper retrying before the first token, structured parsing and its failures, the configuration (key required, base URL, no Anthropic key needed); 2 CLI tests for `check-llm`. Unit suite: 1397 passed. Lint and mypy pass.
- One existing test depended on the developer's own `.env` and failed once `.env` held a key; it now runs in an empty directory.

### Not verified
- **No call has been made to OpenAI.** The adapter has only run against a mock. Whether the key works, whether `gpt-5.6-sol` exists for this account, whether it accepts `reasoning_effort` and `json_schema` as sent, and how long the first-token delay is: all unknown until `covenantiq check-llm` is run.
- **The model name is a guess from a web search** whose sources disagreed (they named `gpt-5.6-sol` and a GPT-6 model). Change `CIQ_LLM_MODEL_DEFAULT` to whatever the account lists.
- **Quality and cost:** the prompts, the citation validator and the evaluation thresholds were written and tuned against Anthropic's models; none of them has run against an OpenAI model. Nothing in M14's offline baseline depends on the provider.
- **Anthropic:** its adapter is unchanged and also never called (no key).

## Reranking is optional (decision recorded)

- Reranking can be switched off (`CIQ_RERANK_PROVIDER=none`: the factory returns nothing, no model or API is used), skipped per request (spec 19.8 `rerank: bool`), and skipped automatically when the reranker fails or times out (the fused order is kept and `rerank_skipped` is recorded in the trace). The evidence gate then uses the stricter RRF-based thresholds (spec 19.10).
- **Decision (user): the default stays `voyage`.** It is not implemented, and startup demands a Voyage key, so a fresh setup must set `CIQ_RERANK_PROVIDER` to `none`, `cohere`, `cross_encoder` or `fake` (`.env.example` documents each). Revisit if first-run friction matters.
- The code that does the fallback and the per-request switch is part of M9 retrieval.

## Cohere reranker (after M8)

- `CIQ_RERANK_PROVIDER=cohere` reranks with Cohere's hosted API (`POST /v2/rerank`); default model `rerank-v5.0-fast` (low latency), `rerank-v5.0-pro` for quality. Facts checked against Cohere's docs (v2 endpoint, `relevance_score` in [0, 1], v5 fast/pro, 32k context). Key from `COHERE_API_KEY` / `CIQ_COHERE_API_KEY` / `CO_API_KEY`; it is a `SecretStr`, never logged or put in an error (tests assert this). Compose passes `COHERE_API_KEY` and `CIQ_RERANK_MODEL` through.
- `ai/rerank/cohere.py` (`CohereReranker`, httpx, not the Cohere SDK) and `ai/rerank/breaker.py` (the circuit breaker, now shared with the Ettin client). Scores are returned as Cohere gives them (no sigmoid). Any failure (timeout, 429, 5xx, rejected key) raises `RerankUnavailable` so retrieval keeps the fused order; 3 failures in a row skip the call for 30 s (a bad key counts, so it cannot make every query call out); 400/404/422 are rejections. Nothing identifying is sent (no client-name header, no ids); passages are cut to 1,024 tokens.
- A model name left at the Voyage default or blank now means "the default for the chosen provider" (Cohere: `rerank-v5.0-fast`, Ettin: the 150M model).
- Tests: 35 new (request shape, score handling, every failure class, breaker, key never leaked, settings, aliases, factory, CLI). the whole unit suite (623 tests) passes; ruff, mypy and import contracts pass. `ADR-018`, the runbook and `.env.example` are updated.
- **Not verified against the real Cohere API: there is no key on this machine.** To verify: put `COHERE_API_KEY` in `.env`, set `CIQ_RERANK_PROVIDER=cohere`, `docker compose up -d`, then `docker compose exec api covenantiq check-rerank`. Latency, rate limits (trial keys), cost per search unit and ranking quality on legal text are therefore unmeasured.
- **Contract text is sent to Cohere** with this provider (the deployment notes must say so).
- With Cohere the 3 s budget is realistic and the spec's 0.55 / 0.35 / 0.30 evidence thresholds are plausible as written (v5 scores are calibrated), unlike the Ettin model; both still need checking on a labelled set.

## Ettin reranker in a separate process (after M8)

### Built
- `CIQ_RERANK_PROVIDER=cross_encoder` reranks with `cross-encoder/ettin-reranker-150m-v1` (Apache 2.0, English only, about 150M parameters). The model runs in its **own process and container**, never in the API or a worker (ADR-017).
- `ai/rerank/`: `Reranker` protocol (spec 19.8 signature), `RerankUnavailable` / `RerankRejected`, `LexicalOverlapReranker` (fake for tests), `RemoteReranker` (HTTP client with a 3 s timeout, scores mapped with a sigmoid into (0, 1), a circuit breaker that skips the call for 30 s after 3 failures in a row), `build_reranker`, and `server.py` (the service).
- The service (`covenantiq rerank-serve`, Compose service `reranker`, profile `rerank`): loads the model once; scores in a thread, one request at a time, so health checks stay responsive; answers 503 at once when more than 3 requests are in flight or queued; takes the caller's deadline, scores in batches of 4 (shortest first) and stops when the next batch would pass the deadline (504), so abandoned work does not hold the CPU. It has no database, broker or API keys and never logs the text it scores.
- The reranker image bakes in the model, pinned to commit `025501c4e0f9...`, and runs offline (`HF_HUB_OFFLINE=1`). Limits: **8 CPUs** and 2 GB by default. `CIQ_RERANK_CPUS` caps the container (Docker's CFS quota, a ceiling and not a reservation) and `CIQ_RERANK_THREADS` is PyTorch's thread count; they must be kept equal. Verified applied: `NanoCpus=8000000000`, cgroup `cpu.max 800000 100000`, 8 threads.
- `covenantiq check-rerank` (ranks four generic sentences, prints order and time). New settings: `CIQ_RERANK_URL`, `CIQ_RERANK_TIMEOUT_SECONDS`, `CIQ_RERANK_MAX_DOCUMENTS`. `sentence-transformers` added as a dependency; it is imported only inside the service.

### Verified
- 586 unit tests pass (59 new for the reranker, plus CLI tests); ruff, mypy and the 13 import contracts pass. A unit test proves that importing the API, the worker runtime and the rerank client does **not** load `torch`, `transformers` or `sentence_transformers` (run in a clean directory and environment).
- Real model, on this machine: loads under sentence-transformers 6.3.0 and ranks correctly (liability cap 8.8, indirect loss 3.0, payment terms -0.7, kitchen -5.3; raw, unbounded scores).
- In the container (first at 4 CPUs, now 8 CPUs, 2 GB): loads offline in about 34 s, uses 1.1 GB; the planted liability-cap passage ranks first for 10, 15 and 30 passages; a 2.7 s deadline is honoured (abort at 2.4 s) and the service carries on. 4 opt-in e2e tests pass (`CIQ_E2E=1 CIQ_E2E_RERANK=1 uv run pytest tests/e2e/test_reranker.py` with `docker compose --profile rerank up -d reranker`).

### Measured speed, and what it means
350-token chunks, CPU only (the model card's 14 pairs/s is for short benchmark passages on 24 threads):

| Setup | 30 passages | Pairs/s |
|---|---|---|
| container, 4 CPUs (old default) | 20.8 s (10: 6.0 s, 5: 3.1 s) | 1.4 |
| **container, 8 CPUs (new default)** | **11.9 s** | **2.5** |
| container, 12 CPUs | 10.2 s | 2.9 |
| container, 16 CPUs (all of them) | 19.7 s | 1.5 |
| host, fp32, 8 threads | 10.5 s | 2.9 |
| host, fp32, 16 threads | 8.8 s | 3.4 |
| int8 dynamic, 16 threads | 6.1 s | 4.9 (rejected, see below) |
| bf16 (no AMX here) | 90 s | 0.3 |

- **More CPU helped, but only up to a point:** 8 CPUs is 1.75x faster than 4 (11.9 s for 30 chunks); 12 gives only 14% more; giving it all 16 is *slower than 4* (19.7 s) because the Docker VM, the workers and the host need cores. Batch size 4 versus 8 made no difference. So 8 is the default and the right size for this machine.
- **On CPU the spec's "top 30 within 3 s" is still not reachable with this model** (about 12 s at best): with the default settings reranking is skipped and the fused order is kept (the safe fallback), and the circuit breaker stops the application asking. Realistic fixes: run the reranker on a GPU host (model card: about 980 pairs/s on an RTX 3090, 3,200 on an H100; only `CIQ_RERANK_URL` changes), give it more CPU, rerank fewer candidates (about 3 to 5 pairs/s per 8 to 16 cores), or switch to a smaller Ettin reranker (68M, 32M, 17M: same code, a different baked-in model). This needs your decision before M9.
- int8 was rejected: it only ran through a path that bypasses sentence-transformers and does not reproduce its scores (the model has extra pooling and dense layers that plain Hugging Face loading drops), for under 2x speed-up.

### Bugs found along the way
- The server returned 500 instead of 413, 422 and 503 (typed response model plus an error dict); fixed with `HTTPException`, found by the tests.
- Offline loading failed in the image: pinning by commit hash writes no `refs/main`, and offline the loader resolves `main` through it. The Dockerfile now writes it.
- A first version of the deadline check started a batch even when it could not finish (a 2.7 s deadline aborted at 4.9 s); it now predicts from the last batch (2.4 s).
- My isolation test depended on the developer's `.env` (importing the API builds the app, which reads `.env`); it now runs in a clean directory and environment.

### Open
- The evidence thresholds in spec 19.10 (0.55, 0.35, 0.30) assume Voyage-like scores; they must be **recalibrated** for this model (`RemoteReranker.rerank_raw` gives the raw scores for it). Not done: the evidence gate does not exist yet.
- Reranking quality for legal text (English-only, general-purpose) is unmeasured.
- The Voyage reranker adapter is not implemented (`CIQ_RERANK_PROVIDER=voyage` raises a clear error).
- The spec's Redis cache of rerank scores (1 h) belongs with retrieval (M9).
- The images are large (backend 11.5 GB, reranker 19.8 GB) because Linux PyTorch pulls CUDA libraries; CPU-only wheels would save several GB. Not changed.

## Docker verification (after M8)

Docker Desktop became available, so everything that was waiting on it was run. All of it against the Compose stack built from this repository, with `CIQ_OFFLINE=1` (fake embeddings, no API keys).

### Verified
- `docker compose build` succeeds (backend image 11.4 GB because of PyTorch and Docling; frontend 820 MB). The `models` stage downloads the layout and table models into the image; the workers load them from `DOCLING_ARTIFACTS_PATH` at start-up and never download at runtime.
- `docker compose up --wait` reaches healthy for postgres, redis, rabbitmq, migrate (exits 0, "database is at head"), api, both workers and streamlit. `psql` shows `vector 0.8.7`; the readiness endpoint reports database, migrations, redis and rabbitmq `ok`.
- **pgvector (closes M3):** on a real `pgvector/pgvector:pg16` database the two migration tests (round trip, models versus migrated schema) and the embedding write test pass, as do the M8 embedding-stage tests writing real `vector(1024)` values.
- **Full suite on real pgvector: 790 passed, 13 skipped (the opt-in e2e tests), 21 slow deselected, 0 failed**; coverage 92%.
- **End to end (`CIQ_E2E=1 uv run pytest tests/e2e`): 13 passed.** A real PDF goes upload, RabbitMQ, worker, Docling on Linux, chunks, embeddings, `ready` (`attempt` 2, one claim per stage, at least 10 chunks). The four work queues are durable with a dead-letter exchange; results live in Redis db 1, never db 0; a scanned PDF fails with `NO_EXTRACTABLE_TEXT`, shows `failed` in PostgreSQL and lands in `document_processing.dlq`; `dlq replay` resets the job, the task runs again (new attempt) and returns to the DLQ.
- **Worker-kill drill (spec milestone 6):** the worker was SIGKILLed mid-extraction, restarted, and the job recovered and finished after the heartbeat was backdated (see bug 3).
- **Real Redis:** five failed logins return 401 and the sixth 429 with `Retry-After`; `redis.conf` (256 MB, `allkeys-lru`) is applied.

### Bugs the real stack found (none visible with fakes) and fixed
1. **RabbitMQ had no user (M2).** `docker/rabbitmq/rabbitmq.conf` loaded `definitions.json`, which defined only the vhost. `load_definitions` replaces the `RABBITMQ_DEFAULT_*` bootstrap, so the broker started with no users and every client got `ACCESS_REFUSED`; the stack committed in M2 could never have run. Fixed by removing `load_definitions` and the mount (the vhost and user now come from `RABBITMQ_DEFAULT_*`, so the password stays in the environment). `definitions.json` is deleted.
2. **`dlq list` and `dlq replay` crashed against RabbitMQ (M6).** A bare channel returns a raw AMQP message with no `.payload`, `.ack()` or `.requeue()`; the in-memory test transport returns kombu messages, which hid it. Messages are now fetched through a bound kombu `Queue`.
3. **A job whose worker died was orphaned forever (M6).** RabbitMQ redelivers the dead worker's message at once, but a heartbeat under 10 minutes old looks alive, so the redelivery was skipped and acknowledged and nothing could resume the job (the sweep only handled `queued`). The sweep now first reaps `running` jobs with a heartbeat over 10 minutes old (`JobService.reap_lost`, a conditional update so a worker that heartbeats in between keeps its job) and republishes them. After 5 lost workers a job fails with `WORKER_LOST`. A job lost with its worker now finishes about 10 to 11 minutes later.
4. The old M6 e2e tests assumed a stub pipeline and a trivial PDF; rewritten for the real two-stage flow. DLQ assertions use the operator CLI instead of the management API, whose queue counts lag by several seconds.

### Notes
- This machine runs a Windows PostgreSQL service on host port 5432, so host-side tools reached that service instead of the container ("password authentication failed" with the correct password). Operator commands and the e2e CLI steps therefore run inside the `api` container (`docker compose exec api covenantiq ...`), and the host-side integration tests used a throwaway pgvector container on port 55432. Documented in the runbook.
- The dev API container logs a `watchfiles ... Cannot allocate memory` error (inotify limit with the bind mount). It only affects auto-reload.
- `docker stats` showed RabbitMQ at 400% CPU for a while after the drill; the real Erlang process used about 2% of a core. Treat it as a Docker Desktop reporting artefact.
- A 7.4 GB Docker allotment is enough for the whole stack; the ingest worker used about 2 GB with Docling loaded. Two heavy documents at once were not tried.

## Deviations from the spec

- Docker verification: the spec's `docker/rabbitmq/definitions.json` (vhost, user, permissions, `guest` removed) is not used. A definitions file cannot read the password from the environment and, when loaded, it suppresses the default-user bootstrap. The vhost and user come from `RABBITMQ_DEFAULT_*`, which also leaves `guest` uncreated.
- Docker verification: lost-worker recovery is done by the sweep (reaping stale `running` jobs and republishing), not only by redelivery as spec 18.10 and 25.4 describe, because the redelivery arrives before the heartbeat is stale and is dropped.
- M8: one processing job covers both stages. After chunking the job is handed on (`running` to `queued`, `stage = embedding`) and `ingest.embed_document` claims it; the spec describes a separate embedding job. The unique "one active job per document" index and the user-facing job id stay simple this way, and a duplicate message for the wrong stage still does the right thing because the stage decides what runs.
- M8: provider throttling relies on the task's `rate_limit` (30/m) plus retry with backoff on 429, not on the Redis token bucket from the rate limiter; the bucket is fixed-window and would have meant waiting inside the task.
- M8: `document_clauses.page_*` and `document_chunks.page_*` became nullable (migration 0006) because DOCX has no pages; the spec had them not null.
- M8: the sweep measures how long a queued job has been idle from its last heartbeat (falling back to `created_at`), so a job freshly handed to the embedding stage is not re-published at once; added `JobService.hand_off` and `JobService.release`.
- M8: `CovenantTask` now releases a job (`running` to `queued`) before every retry. Before this, a retried delivery found the job still `running` with a fresh heartbeat, was told "not claimable", and silently ended; the job then sat `running` until nobody was left to take it over. This affected M6/M7 tasks as well.
- M8: new setting `CIQ_CLAUSE_HEADING_DEPTH` (1 to 3, default chosen per document), and `config/clause_taxonomy.yaml` for clause-type hints.
- M7: `ExtractedBlock.page` is `int | None` (spec: `int`) because DOCX has no pages; blocks also carry `injection_score` and `injection_flags`, which chunking will need.
- M7: the models stage names `layout` and `tableformer` instead of downloading the default set (which includes speech, VLM and OCR models we do not use).
- M7: the spec's M7 stops at "stores extraction quality"; the job is closed there (document left at `chunking`) so uploads do not hold the per-user active-job limit until M8 exists.
- M6: `WorkerContainer` lives in `workers/runtime.py`, not `core/container.py` (the import rules forbid `core` from importing the layers it wires).
- M6: `ingest.process_document` marks the document `ready` with no chunks, so the surrounding plumbing can be exercised end to end; it is a stub until M7.
- M6: the task retry loop is implemented in `CovenantTask` rather than `autoretry_for`, so database errors and the one-time timeout retry share one path, and the whole behaviour is testable with Celery's eager mode.
- M6: new setting `CIQ_SWEEP_INTERVAL_SECONDS` (not in spec section 35).
- M5: `storage_path` is stored relative to the storage root (`owner/document/sha.ext`), not as an absolute path, so the root can move or become an object-store prefix.
- M5: a plain `application/zip` sniff is accepted for `.docx` (libmagic often reports real DOCX files that way); the structure probe then confirms it is a genuine, macro-free DOCX.
- M5: `FILE_TOO_LARGE` is always HTTP 413 (also for the zip-bomb guard); added the `ENCRYPTED_PDF` and `CONFLICT` error codes.
- M5: added `BodySizeLimitMiddleware`. Starlette parses a whole multipart body before the endpoint runs, so a limit enforced only in the service would let an oversized file be spooled to disk first.
- M5: test fixtures are generated in code (`tests/fixtures/contracts.py`, PDFs via the dev-only `pypdf`) instead of committed binary files.
- M5: vendor detail returns the vendor and its documents; the latest-assessment summary is added in M13.
- M5: document and vendor repositories use keyset (`after`) pagination instead of offsets; `TimestampMixin` models now fetch `updated_at` eagerly so async code can read it after an UPDATE.
- M4: `Container` lives in `api/container.py`, not `core/container.py`; the spec's own import rules forbid `core` from importing the layers the container wires together.
- M4: added `POST /auth/token` (OAuth2 form) beside the JSON `/auth/login`, so the Authorize button in `/docs` works.
- M4: added the `INVALID_CREDENTIALS` error code (spec 30.1 names it but the 16.1 table omits it); unknown HTTP errors use plain string codes (`NOT_FOUND`, `METHOD_NOT_ALLOWED`, `HTTP_ERROR`).
- M4: the rate limiter fails open when Redis is unavailable. The spec is silent; the alternative would lock everyone out of login whenever Redis blips.
- M4: no CORS middleware yet; Streamlit calls the API server-side, so it is not needed until a browser client exists.
- M3: citations are created in migration 0005, not 0004, because they reference findings and risk dimensions.
- M3: `document_processing_jobs` has a `created_at` column (not in the spec's ER diagram), needed for the stuck-job sweep.
- M2: the backend image has no ENTRYPOINT. The spec says the entrypoint is the `covenantiq` CLI, but its own Compose commands start with `uvicorn` and `celery`, which would break.
- M2: the Docling model-download stage is deferred to M7, when the dependency is added.
- M2: the API health check uses `/health/live` until readiness checks exist.
- Only M1-relevant dependencies were added (pydantic, pydantic-settings, structlog, typer, plus dev tools). The rest are added in the milestone that uses them.
- `enums.py` holds only `Environment` and `LogFormat`; status enums arrive with the ORM models in M3.

## Open items and next steps

### What is left, by who can do it
- **Needs an embedding API key (Voyage, or OpenAI as chosen):** one real embedding call and a small ingest of a real contract with `CIQ_OFFLINE=0`. OpenAI `text-embedding-3-small` is supported (adapter requests `dimensions=1024`); to verify, put `OPENAI_API_KEY` in `.env` with the settings in the runbook ("Using OpenAI instead of Voyage"), run `docker compose up -d`, then `docker compose exec api covenantiq check-embeddings`. Not yet run: there is no key on this machine.
- **Needs real contracts:** check Docling extraction, clause boundaries and chunk sizes on 3 to 5 real agreements (the tests use generated fixtures).
- **Needs CI:** a job that caches the Docling models and runs the slow suite (`uv run pytest -m slow`), and a Compose smoke job (spec 33). The slow suite was last run on Windows in M7 and has not been run since M8.
- **Not tried:** two large documents processed at the same time on the 7.4 GB Docker allotment; production Compose (`docker-compose.prod.yml` is a skeleton until M19).
- **Needs your decision:** whether to push (everything is committed locally; nothing has been pushed).
- **Needs an Anthropic key:** `CIQ_E2E_LLM=1 uv run pytest tests/e2e/test_anthropic_live.py` (three short calls), then one real question over an ingested contract to see answer quality, citation validity and cost. Not run: there is no key on this machine.
- **RabbitMQ coverage gap (M11 to M13 added to it; chat does not use it, analysis and vendor tasks do):** RabbitMQ is the Celery broker for ingestion, embedding, purge, dead-lettering and contract analysis (chat does not use it: replies are an asyncio task in the API). It was run for real only in M6 (dead-lettering, replay, worker-kill drill). Since then tasks have mostly been tested eagerly in-process. Not yet run: ingestion, embedding and the analysis task delivered through the Compose broker to the Compose workers, and killing a real worker mid-analysis. Needs the backend image rebuilt first, then `CIQ_E2E=1 uv run pytest tests/e2e` plus one analysis through the broker.
- **Needs an Anthropic key (analysis):** analyse one real contract and read the findings end to end (cost, time, false positives, and the share of rules ending `insufficient_evidence`); tune `config/rules/*` and the `analysis:` thresholds from what it shows.
- **Needs an Anthropic key (vendors):** assess one real vendor with two or three real contracts and read the dimension reasoning, the `notes`, and a comparison; check how many signals end as gaps and whether the extraction signals are noisy.
- **Needs a run of `covenantiq check-llm`** (uses the key in `.env`, costs a few tokens): the first real model call of the project; then ask a real question in the UI.
- **Needs an Anthropic key (evaluation):** `make eval-full` (cost estimate printed first), then commit the report under `docs/eval/`; calibrate the evidence thresholds from it and add `eval-retrieval`.
- **Needs your decision:** whether an answer made only of `ungrounded` claims should abstain (see M14).
- **Needs an Anthropic key (streaming):** ask one real question over an ingested contract and watch it with `curl -N` (time to first token, how the draft differs from the final text, whether citations validate).
- **Needs a proxy to try:** the SSE endpoint behind nginx or a load balancer (buffering, idle timeout).
- **Needs a browser:** open http://localhost:8501 and click through every page (layout, citation links, charts, upload, sign out).
- **Needs the Compose stack and a key:** the whole path through the UI, with documents, analysis, two vendors and a comparison (the acceptance criterion of M16).
- **Next milestone:** M18. M9 to M17 are complete but unmeasured on real data (see their "Not yet verified" sections).

- Commits on `main`, none pushed: `a5423cd` (M1, M2), `5a18552` (M3), `22c779e` (M4), `7d8411e` (M5 to M9 with the Docker fixes, OpenAI embeddings, Ettin), `22f2e8a` (M10), `72d8c56` (M11 to M13), `e63ecfe` (M14), `e111890` (M15), `6f12c09` (M16). M17 is committed after it (see `git log`).
- Slow tests (real Docling): `uv run pytest -m slow` (many minutes on the first run while models load; last run in M7).
- Next: M18.
