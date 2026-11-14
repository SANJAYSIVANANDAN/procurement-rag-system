# CovenantIQ — Progress

Source of truth: [PROJECT_SPEC.md](PROJECT_SPEC.md). Milestones are defined in §36.

## Status

| Milestone | Status |
|---|---|
| M1 — Repository setup and architecture skeleton | Done and committed |
| M2 — Docker infrastructure | Committed, with fixes made after the first real run (RabbitMQ bootstrap, see "Docker verification"); stack builds and is healthy. The M2 fixes are uncommitted |
| M3 — PostgreSQL and SQLAlchemy | Done and committed; the pgvector tests now pass on a real pgvector database |
| M4 — Authentication | Done and committed; Redis rate limiting and RabbitMQ readiness verified against the real stack |
| M5 — Document management | Code and tests complete, not committed; libmagic verified on Windows and in the Linux image (via the e2e upload) |
| M6 — Celery and RabbitMQ | Code and tests complete, not committed; real-broker run found and fixed two bugs (DLQ commands, lost-worker jobs); dead-lettering, replay and the worker-kill drill verified |
| M7 — Document extraction | Code and tests complete, not committed; verified with real Docling on Windows and on Linux (inside the image) |
| M8 — Chunking and embeddings | Code and tests complete, not committed; vector writes verified on real pgvector; the real Voyage API is unverified |
| M8+ — OpenAI embeddings, Ettin and Cohere rerankers | Code and tests complete, not committed; none verified against the real hosted APIs (no keys); Ettin reranker verified in its container |
| M9 — pgvector and hybrid retrieval | Code and tests complete, not committed; verified on real pgvector with fake embeddings and a fake reranker; never run with real embeddings, a real reranker or real contracts |
| M10–M19 | Not started |

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
- **Needs your decision:** commit M5 to M8 and the Docker fixes (all uncommitted; nothing has been pushed).
- **Next milestone:** M10 (Redis session memory); M11 (LangGraph chat) builds on the M9 retrieval engine. M9 is complete but unmeasured on real data (see "M9 — Not yet verified").

- M1 and M2 are committed locally on `main`; nothing has been pushed.
- M5 to M8, the Docker fixes (RabbitMQ bootstrap, DLQ commands, lost-worker reaper, e2e tests, runbook), OpenAI embedding support and the Ettin reranker are all uncommitted.
- Slow tests (real Docling): `uv run pytest -m slow` (expect many minutes on the first run while models load).
- Next: M9 — Hybrid retrieval (vector + full-text search over chunks, reciprocal rank fusion, clause-type filters, parent expansion).
