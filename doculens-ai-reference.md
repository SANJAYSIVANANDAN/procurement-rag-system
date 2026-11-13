# DocuLens AI — Tech Stack & Implementation Reference

A complete technical breakdown of the [DocuLens AI](https://github.com/CodeWithMoin/doculens-ai) open-source document intelligence system, extracted for inspiration and reference.

---

## 1. Tech Stack at a Glance

| Layer | Technology | Purpose |
| --- | --- | --- |
| API | FastAPI + Pydantic + SQLAlchemy | HTTP contracts, validation, auth, persistence |
| Task queue | Celery + Redis | Async document & AI processing |
| Document parsing | Docling 2.95 | Layout-aware PDF/text extraction |
| Chunking | tiktoken | Token-bounded chunk splitting |
| Embeddings | OpenAI Embeddings API | Dense vector creation |
| LLM providers | OpenAI, Anthropic, OpenRouter, local OpenAI-compatible endpoints | Classification, QA, summarization |
| Structured outputs | Instructor 1.4 | Provider-neutral Pydantic-validated LLM responses |
| Database | PostgreSQL 16 via TimescaleDB-HA | Relational metadata + hypertables |
| Vector search | pgvector + DiskANN via timescale-vector 0.0.7 | Dense ANN retrieval |
| Keyword search | TimescaleDB full-text search | Hybrid retrieval complement |
| Frontend | React 19 + TypeScript + Vite + TanStack Query | Operator console |
| Reverse proxy | Caddy | TLS termination + routing |
| Containerization | Docker Compose | Full local + showcase stack |
| Auth | JWT (python-jose) + bcrypt (passlib) + optional API keys | Persona-based access control |
| Migrations | Alembic 1.13 | Schema versioning |
| CI | GitHub Actions | Lint, type-check, test on push |
| Quality tools | Ruff, Pyright, pytest, pre-commit | Code quality gates |

---

## 2. Backend Python Dependencies (Exact Versions)

All pinned in `pyproject.toml`:

| Package | Version | Role |
| --- | --- | --- |
| `fastapi` | 0.111.1 | Web framework |
| `pydantic` | 2.10.4 | Data validation & serialization |
| `pydantic-settings` | 2.7.0 | Env-var config management |
| `sqlalchemy` | >=2.0, <3 | ORM |
| `alembic` | 1.13.1 | DB migrations |
| `psycopg2-binary` | 2.9.9 | PostgreSQL driver |
| `celery` | 5.4.0 | Distributed task queue |
| `redis` | 5.0.3 | Celery broker + result backend |
| `docling` | 2.95.0 | Document extraction |
| `tiktoken` | >=0.8, <1 | Token counting & chunking |
| `openai` | >=1.40, <2 | Embedding + LLM calls |
| `anthropic` | 0.31.2 | Anthropic Claude calls |
| `instructor` | 1.4.0 | Structured LLM outputs |
| `timescale-vector` | 0.0.7 | pgvector / DiskANN client |
| `uvicorn[standard]` | >=0.30, <1 | ASGI server |
| `python-jose[cryptography]` | 3.3.0 | JWT signing & verification |
| `passlib[bcrypt]` | 1.7.4 | Password hashing |
| `bcrypt` | 3.1.7 | bcrypt backend |
| `python-multipart` | >=0.0.18, <1 | File upload parsing |
| `python-frontmatter` | 1.1.0 | Jinja prompt asset metadata |
| `pandas` | 2.2.3 | Evaluation metrics & data handling |
| `requests` | >=2.32, <3 | HTTP client (internal) |

---

## 3. Architecture & Request Flow

```
[React Console / API Client]
        |
        v
[FastAPI Gateway -- /api/v1]
        |  (1) persist event to TimescaleDB
        |  (2) enqueue event ID to Redis
        v
[Redis Broker]
        |
        v
[Celery Worker]
  (3) validate event from DB
  (4) select pipeline from registry
  (5) run pipeline nodes:
     - Docling extraction
     - Layout-aware chunking (tiktoken)
     - Batched embedding (OpenAI)
     - Structured LLM call (Instructor)
  (6) write vectors -> pgvector/DiskANN
  (7) write results -> PostgreSQL
        |
        v
[PostgreSQL + TimescaleDB + pgvector]
```

Key design decisions:

- The **event record** is the durable boundary between HTTP and AI work. Each Celery task reads the stored event, making every input/output fully auditable.
- Pipeline nodes are **synchronous** inside the worker; parallelism comes from worker concurrency (not async).
- **Late acknowledgement** + bounded task time + worker-loss recovery make tasks safe to retry without duplicate effects.
- The gateway returns **202 Accepted** immediately after enqueuing; the client polls for status.

---

## 4. Document Processing Pipeline

### 4.1 Ingestion

1. User uploads PDF or text via `POST /api/v1/documents`
2. FastAPI validates with Pydantic, stores an event record in TimescaleDB
3. Event ID is enqueued to Redis
4. HTTP returns 202 Accepted immediately

### 4.2 Extraction (Docling)

- **Docling 2.95** runs inside the Celery worker
- Performs **layout-aware extraction** — understands page structure, tables, headings, columns
- EasyOCR is bundled for scanned PDFs (cache dir: `/home/celery/.EasyOCR`)
- Docling model cache: `/home/celery/.cache/docling`

### 4.3 Chunking (tiktoken)

- Extracted text is split into **token-bounded chunks**
- Default chunk size: **800 tokens** (env var `DOCULENS_CHUNK_MAX_TOKENS`)
- Each chunk carries metadata: document id, chunk id, page number, section title, token count
- Metadata is stored alongside the vector for filtered retrieval at query time

### 4.4 Embedding (OpenAI)

- Chunks are embedded in **batches** (`DOCULENS_EMBEDDING_BATCH_SIZE`)
- A **process-local in-memory cache** (`DOCULENS_EMBEDDING_CACHE_SIZE`) deduplicates repeated text within a worker's lifetime (resets on deploy, never durable)
- Vectors stored in **pgvector / DiskANN** via the timescale-vector client

### 4.5 Retrieval

- Query is embedded at request time using the same OpenAI model
- **Metadata-filtered semantic search** (dense, ANN via DiskANN) retrieves top-K chunks (`DOCULENS_QA_TOP_K`, default 5)
- **Keyword search** (TimescaleDB full-text) is available as a complement for hybrid retrieval
- Retrieved chunks (with provenance: document, chunk, page) are passed to the LLM prompt

### 4.6 LLM Calls (Instructor + providers)

- **Instructor 1.4** wraps OpenAI / Anthropic / OpenRouter clients to enforce **Pydantic-validated structured outputs**
- Prompts are **Jinja2 templates** stored as versioned assets in `app/prompts/` with frontmatter metadata
- Supported tasks: classification with configurable label hierarchies, structured information extraction, summarization with source chunk provenance, QA with citations and confidence scores
- Provider timeout: 30 seconds default (`DOCULENS_PROVIDER_TIMEOUT_SECONDS`)

---

## 5. Data Layer

| Store | Technology | What lives here |
| --- | --- | --- |
| Relational + time-series | PostgreSQL 16 + TimescaleDB-HA | Document metadata, event log, task results, user/auth records |
| Vector index | pgvector + DiskANN (via timescale-vector) | Chunk embeddings with per-chunk metadata |
| Full-text | TimescaleDB built-in | Keyword search index |
| Job queue | Redis | Celery task queue + result backend |

Schema management: Alembic for production migrations; automatic table creation (`DOCULENS_INITIALIZE_DATABASE=true`) for local development only.

---

## 6. API Design

- Base path: `/api/v1` (canonical); legacy `/events` routes kept for backwards compatibility
- OpenAPI docs auto-generated at `/docs`
- Health check: `GET /health/live`
- Auth: JWT bearer tokens + optional API key header
- File upload limit: 25 MiB default (`DOCULENS_MAX_UPLOAD_BYTES`)
- CORS origins configured via `DOCULENS_CORS_ORIGINS`
- Response on upload: **202 Accepted** (async processing); clients poll for results

**Internal module layout (`app/api/`):**

- HTTP contracts & Pydantic schemas
- Auth dependencies (JWT decode, API key validation)
- Versioned routers (v1 canonical)

---

## 7. Frontend (React Operator Console)

| Technology | Version / Detail |
| --- | --- |
| React | 19 |
| TypeScript | Strict mode |
| Build tool | Vite |
| Data fetching | TanStack Query (react-query) |
| Code splitting | Route-level lazy loading |
| Dark mode | Persistent (localStorage) |
| Accessibility | Reduced-motion fallbacks |

The console exposes: document upload, work queues, task notifications, dashboards, request history, and showcase mode (read-only demo with synthetic data).

---

## 8. Infrastructure & Docker Stack

### Services

| Service | Image / Build | Host Port | Notes |
| --- | --- | --- | --- |
| `frontend` | Custom Dockerfile | `127.0.0.1:5173` | Nginx-served React build |
| `api` | Custom Dockerfile | `127.0.0.1:8080` | FastAPI + uvicorn |
| `celery_worker` | Custom Dockerfile | none | Background AI processing |
| `database` | `timescale/timescaledb-ha:pg16` | `127.0.0.1:5432` | PostgreSQL + TimescaleDB + pgvector |
| `redis` | `redis:latest` | `127.0.0.1:6379` | Broker + result backend |
| `caddy` | `caddy:latest` | 80, 443, 2019 (admin) | Reverse proxy + TLS |

### Named Volumes

- `postgres_data` — database persistence
- `redis_data` — Redis persistence
- `ingestion_data` — shared between api and celery_worker (`/workspace/data/ingestion`)
- `easyocr_cache` — OCR model cache
- `docling_cache` — Docling model cache
- `caddy_data` / `caddy_config` — TLS certificates and Caddy config

### Health Checks

| Service | Check | Interval | Retries |
| --- | --- | --- | --- |
| api | `curl /health/live` | 10s | 5 |
| database | `pg_isready` | 5s | 10 |
| redis | `redis-cli ping` | 30s | 5 |

Celery worker waits for database + redis health before starting. All ports except Caddy 80/443 are bound to `127.0.0.1` only.

---

## 9. Auth & Security

- **JWT tokens** signed with `DOCULENS_AUTH_SECRET` (python-jose + cryptography)
- **bcrypt** password hashing via passlib
- **Persona-based access:** multiple JWT roles for different operator levels
- **API key** support as an alternative auth method
- Single workspace only — no multi-tenant authorization
- Production safety checks gated on `DOCULENS_ENVIRONMENT=production`
- Demo users opt-in (`DOCULENS_SEED_DEMO_USERS=true`), disabled in production
- Public read-only showcase mode (`DOCULENS_SHOWCASE_READ_ONLY=true`) blocks all state-changing requests

---

## 10. Key Configuration Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOCULENS_ENVIRONMENT` | — | Enables production safety checks |
| `DOCULENS_CORS_ORIGINS` | — | Allowed browser origins |
| `DOCULENS_MAX_UPLOAD_BYTES` | 25 MiB | Upload size limit |
| `DOCULENS_CHUNK_MAX_TOKENS` | 800 | Max tokens per chunk |
| `DOCULENS_EMBEDDING_BATCH_SIZE` | — | Embedding request batch size |
| `DOCULENS_EMBEDDING_CACHE_SIZE` | — | In-memory embedding cache entries |
| `DOCULENS_PROVIDER_TIMEOUT_SECONDS` | 30 | LLM provider call timeout |
| `DOCULENS_QA_TOP_K` | 5 | Chunks retrieved per QA query |
| `DOCULENS_SHOWCASE_READ_ONLY` | false | Enables read-only showcase mode |
| `DOCULENS_INITIALIZE_DATABASE` | false | Auto table creation (dev only) |
| `DOCULENS_SEED_DEMO_USERS` | false | Seeds demo user accounts |
| `OPENAI_API_KEY` | required | OpenAI embeddings + LLM |
| `DOCULENS_AUTH_SECRET` | required | JWT signing secret |
| `DATABASE_NAME / USER / PASSWORD` | required | PostgreSQL credentials |

Template files: `.env.example` (full local setup) and `.env.showcase.example` (showcase deployment).

---

## 11. Codebase Structure

```
doculens-ai/
├── app/
│   ├── api/          # HTTP contracts, auth, deps, versioned routers
│   ├── config/       # Pydantic-settings runtime + infrastructure config
│   ├── core/         # Pipeline primitives, observability
│   ├── database/     # SQLAlchemy models, repositories, Alembic
│   ├── doc_utils/    # Extraction, chunking, embedding, retrieval
│   ├── evaluation/   # Offline RAG metrics (Recall@K, MRR)
│   ├── pipelines/    # Typed document & QA workflow definitions
│   ├── prompts/      # Jinja2 prompt assets (versioned with frontmatter)
│   ├── services/     # Domain + external provider adapters
│   └── tasks/        # Durable Celery task entrypoints
├── frontend/         # React 19 / TypeScript / Vite operator console
├── docker/           # Dockerfiles + docker-compose.yml + Caddyfile
├── tests/            # API, pipeline, dashboard, evaluation tests
├── requests/         # Executable example payloads
├── docs/             # Engineering notes, design system, showcase runbook
├── scripts/          # Dev/ops helper scripts
├── playground/       # Experiment space
├── data/             # Sample documents
├── config/           # Additional configuration
├── pyproject.toml    # Python deps + tool config (Ruff, Pyright, pytest)
├── Makefile          # Developer commands
└── .env.example      # Full local environment template
```

---

## 12. Showcase Mode

A read-only public demo mode for sharing the system without exposing live ingestion:

- Seeds **synthetic document data** idempotently on startup
- Skips the Celery worker entirely (pre-seeded results served directly)
- Blocks all state-changing API requests
- Configured via `.env.showcase.example` and `DOCULENS_SHOWCASE_READ_ONLY=true`
- Started with `make showcase-up`

---

## 13. Evaluation & Quality

- **Offline retrieval evaluation:** Recall@K and MRR metrics in `app/evaluation/` using pandas
- **Tests:** pytest 8.3.3 covering API endpoints, pipelines, dashboards, and evaluation logic
- **Coverage:** pytest-cov with reporting
- **Type checking:** Pyright >=1.1.400 (strict) on every commit
- **Linting + formatting:** Ruff >=0.12 (replaces flake8 + black)
- **CI:** GitHub Actions (`backend-ci.yml`) runs all checks on every push
- **Pre-commit hooks:** Ruff + Pyright before every commit via pre-commit 4.x
- **HTTP testing:** httpx async test client

---

## 14. Known Limitations & Roadmap

**Current limitations:**

- OCR and model accuracy vary by document quality and provider
- Retrieval is dense-first; hybrid ranking (RRF) needs a labelled corpus to tune
- Auth supports a single workspace only — no multi-tenant authorization
- Deployment manifest is scoped to a single-node showcase
- Embedding cache is process-local and resets on deploy (not durable or shared)

**Roadmap:**

- Persist event lifecycle states (pending → processing → done/failed)
- Add a labelled retrieval benchmark dataset
- Evaluate reciprocal-rank fusion (RRF) for hybrid dense+keyword ranking
- OpenTelemetry tracing integration
- Split the legacy event module into separate bounded contexts
- Publish signed container images

---

## 15. Developer Quick-Start

```bash
make install       # install Python deps (editable)
make up            # start the full Docker stack
make check         # run lint + type check + tests
make showcase-up   # start the read-only showcase stack
make help          # list all available commands
```

| Endpoint | URL |
| --- | --- |
| Operator console | http://localhost:5173 |
| API | http://localhost:8080 |
| OpenAPI docs | http://localhost:8080/docs |
| Health check | http://localhost:8080/health/live |

---

## 16. Dev Dependencies

| Package | Version | Role |
| --- | --- | --- |
| `pytest` | 8.3.3 | Test runner |
| `pytest-cov` | >=6, <7 | Coverage reporting |
| `httpx` | >=0.27, <1 | Async HTTP test client |
| `pyright` | >=1.1.400, <2 | Static type checker |
| `ruff` | >=0.12, <1 | Linter + formatter |
| `pre-commit` | >=4, <5 | Git hook runner |

Build system requires `setuptools >= 75`.
