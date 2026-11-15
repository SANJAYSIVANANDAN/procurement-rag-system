# CovenantIQ V1: what was built

This is the specification of **version 1 as it exists**, not as it was planned. `PROJECT_SPEC.md` is the original plan (5,000 lines). `progress.md` is the milestone-by-milestone log. This file is the short, true summary: what V1 is, how it works, what was checked, what was not, and where it differs from the plan.

Status at the time of writing: **M1 to M17 done and committed locally (nothing pushed). M18 (CI/CD) and M19 (production Docker deployment) are not started.**

---

## 1. What V1 is

A web application for reviewing procurement contracts. A signed-in user uploads contracts (PDF or Word), and the system:

1. **Reads and indexes** them (extraction, clause and chunk building, embeddings).
2. **Answers questions** about them in chat, with citations that are checked against the source text, and **declines** when the documents do not contain the answer.
3. **Analyses a contract** against a rulebook of 30 clause rules, producing findings with a status, severity, confidence and the evidence behind each.
4. **Assesses a vendor** across all of that vendor's contracts: a deterministic risk score over 6 dimensions.
5. **Compares vendors** side by side, only where the evidence supports a comparison.

Every user sees only their own data. Output is decision support, not legal advice, and findings carry a disclaimer.

**Not in V1:** public sign-up flows beyond email and password, refresh tokens, roles or teams, object storage, multi-language documents, email, a mobile app, billing.

---

## 2. Architecture

**A modular monolith** (ADR-001): one Python codebase, one package (`covenantiq`), several runtime containers. Layering is enforced by import-linter (15 contracts) so the boundaries cannot erode.

```
Browser -> Streamlit UI (frontend/)  -- HTTP --> FastAPI (api)  --> services --> repositories --> PostgreSQL (+pgvector)
                                                     |                |
                                                     |                +--> Redis (cache, stream, locks, rate limits)
                                                     +--> RabbitMQ --> Celery workers (ingest, analysis) --> same services
                                                     +--> asyncio task: chat graph --> Redis Stream --> SSE to the browser
                                    reranker container (optional) <-- HTTP -- retrieval engine
```

### Runtime containers (Docker Compose)
| Service | Role |
|---|---|
| `postgres` | PostgreSQL 16 with pgvector, pg_trgm, citext. The source of truth. |
| `redis` | Cache, stream buffer, locks, rate limits. Celery results use DB 1. |
| `rabbitmq` | Celery broker for background jobs. |
| `migrate` | One-shot Alembic upgrade (and LangGraph checkpoint tables). |
| `api` | FastAPI, uvicorn with 2 workers. |
| `worker-ingest` | Celery: document processing (Docling), embedding, purge. |
| `worker-analysis` | Celery: contract analysis, vendor assessment, comparison. |
| `reranker` | Optional cross-encoder model in its own process. |
| `streamlit` | The web UI. |

### Python packages (`src/covenantiq/`)
`core` (settings, enums, errors, logging, security, Redis keys) / `db` (models, repositories, unit of work) / `ingestion` (extraction, chunking, scanning) / `ai` (LLM, embeddings, reranker adapters) / `rag` (retrieval, evidence gate, citation validation) / `analysis` (pure contract and vendor engines) / `graphs` (LangGraph workflows) / `memory` (conversation memory) / `streaming` (SSE and locks) / `services` (use cases) / `workers` (Celery) / `api` (FastAPI). Each has a `logic.md` explaining it. The UI is the separate top-level package `frontend/`, which may not import the backend (and vice versa).

### Core design decisions (see `docs/adr/`, 27 records)
- **PostgreSQL does everything data-related**: relational data, vectors (HNSW), full-text search. No separate vector database (ADR-002).
- **Redis is a cache, never the truth.** Losing it costs speed, not correctness.
- **RabbitMQ carries background jobs only; chat does not use it.** A chat reply runs as an asyncio task inside the API and streams through a Redis Stream (ADR-007).
- **The model reads; code decides.** The LLM proposes categories and quotes; statuses, severities, scores and rankings are deterministic code (ADR-009).
- **Ports and adapters for every model**, so vendors are configuration and tests use fakes (ADR-011, ADR-012).
- **Ownership is enforced in every query** via an `OwnershipScope`; a foreign id is always a 404.

---

## 3. Features as built

### 3.1 Accounts and security
- Register, login (JSON and OAuth2 form), `me`. Argon2 password hashing with rehash on login; password policy (12 to 128 chars, common-password list). JWT access tokens only, HS256 (ADR-015).
- Redis fixed-window rate limits: login 10/min per IP and 5/min per email, chat submit 20/min, upload 30/hour, analysis start 20/hour, default 300/min. The limiter fails open if Redis is down.
- Security headers, `X-Request-ID`, size-limited uploads (rejected mid-stream with 413), one error envelope for every failure.

### 3.2 Documents and ingestion
- **Upload validation**: extension, content sniffing, structure probe, size, encrypted PDF, macro-enabled DOCX, page count, zip-bomb guard. A duplicate (same file for the same user) returns the existing document.
- **Pipeline (worker)**: Docling extraction, heading-stack and clause-reference recovery, header/footer removal, quality check, **parent clauses** (up to ~1500 tokens) and **child chunks** (target 350, max 500, 60 overlap; legal-aware sentence splitting), clause-type tagging from `config/clause_taxonomy.yaml`, **prompt-injection scan**, embeddings (1024-dim, batches of 64).
- **Statuses** with progress and stage labels; one job covers both the processing and embedding stages (hand-off); failures give a specific code and message. Re-embedding service for model changes.
- Contract types: `msa, saas, dpa, sow, nda, license, supply, services, other, unknown`.

### 3.3 Chat (RAG)
- **Hybrid retrieval**: dense (pgvector cosine) top 30 + lexical (PostgreSQL FTS) top 30, fused by **RRF** (k=60, 1.1 boost for the asked-for clause type), optional rerank of the top 30 (3 s budget), top 8, expanded to parent clauses.
- **Evidence gate**: strong / moderate / weak / none; weak or none returns a fixed refusal with **no model call**.
- **Grounded generation** with numbered `<source>` blocks and `[C#]` markers; sources are escaped and declared untrusted; a per-request **canary** in the system prompt detects leaked instructions.
- **Citation validator** (deterministic): markers must exist, cited sentences must be supported by their source, numbers must appear in the source, uncited figures are flagged. Outcomes: shown, shown with a notice, or replaced by abstention.
- **Intent routing**: greetings and out-of-scope questions are answered directly or declined; follow-ups are rewritten into standalone queries.
- **Memory**: Redis window + rolling summary (summaries in PostgreSQL), rebuilt from PostgreSQL whenever Redis is wrong or empty.
- **Streaming**: SSE with Redis Stream entry ids as event ids, `Last-Event-ID` resume, replay from PostgreSQL after expiry, stall detection, token coalescing, one reply at a time per conversation (Redis lock with heartbeat; 409 if busy).
- **Persistence**: conversations (scope to chosen documents, rename, archive), messages with sequence numbers, citations, grounding status, idempotent resubmit.

### 3.4 Contract analysis
- A **rulebook** of 30 rules (`config/rules/contract_baseline_v1.yaml`) over 15 risk categories (liability, indemnification, termination, payment, renewal, IP, data protection, security, SLA, confidentiality, compliance, dispute resolution, assignment, insurance, general). Each rule says which contract types it applies to.
- Per rule: retrieval, batched model assessment (4 rules per call), deterministic red-flag regexes, then **status decision in code**: `present / partial / absent / insufficient_evidence`. A clause is `absent` only with evidence of a thorough search; otherwise `insufficient_evidence`.
- Also: obligations and deadlines, internal contradictions (candidates found deterministically, model confirms), unusual provisions, evidence validation. Findings include quotes, page and clause references, severity, confidence and a disclaimer.
- **Checkpointed** per job in PostgreSQL: a dead worker resumes without repeating finished batches.

### 3.5 Vendor assessment and comparison
- **Assessment**: ensures contract analyses exist, maps findings to **26 signals** (`config/vendor/signals_v1.yaml`) in **6 dimensions** (legal 0.25, data security 0.25, operational 0.20, financial/commercial 0.15, compliance 0.10, dependency 0.05), the model extracts signals per dimension and explains each, **code scores** them (0 to 100, higher is riskier). Absence is a gap that lowers completeness, not the score, except for three named signals where absence is itself a risk (no liability cap, no breach notice, no exit assistance). Weights renormalise over assessed dimensions. Assessments are marked **provisional** when completeness is under 0.6 or fewer than 4 dimensions are assessed. Severity bands: <25 low, <50 medium, <75 high, else critical.
- **Comparison**: compares only where both vendors have evidence, never ranks an absence against an established clause, suppresses the ranking unless every assessment is complete enough, checks configuration compatibility (409 if incompatible), and attaches a model-written narrative that is validated against the matrix (template fallback; the job never fails on narrative).

### 3.6 Web UI (Streamlit, nine pages)
Login, dashboard, documents, upload, chat (live streaming, citations, grounding badge), analysis (finding cards, export), vendors, compare (matrix), history. The UI talks to the API only over HTTP and follows SSE with resume.

### 3.7 Operations
- CLI (`covenantiq`): `migrate`, `check-llm`, `check-embeddings`, `dlq list/replay`, `reembed`, `rerank-serve`, and others.
- **Reliability**: CAS job claiming, 30 s heartbeat with 10 minute takeover (max 5 lost attempts), 60 s sweeper for the commit-then-publish gap, `acks_late` + `reject_on_worker_lost`, retries with backoff, dead-letter queues per work queue with replay, queue-depth backpressure for ingestion.
- **Observability**: structured JSON logs with a request id, secret redaction, no local variables in tracebacks. Health: `/health/live`, `/health/ready` (database, migrations, Redis, RabbitMQ).
- **Runbook**: `docs/runbook.md`.

---

## 4. Data and interfaces

### Main tables (6 Alembic migrations)
`users`, `vendors`, `documents`, `document_clauses` (parents), `document_chunks` (children + vector + tsvector), `document_processing_jobs`, `conversations`, `messages`, `citations`, `analysis_jobs`, findings and risk-dimension tables, `vendor_risk_assessments`, comparisons; plus LangGraph checkpoint tables. Primary keys are UUIDv7.

### API (`/api/v1`)
- **Auth**: `POST /auth/register`, `/auth/login`, `/auth/token`; `GET /auth/me`.
- **Documents**: `POST /documents/upload`; `GET /documents`, `/{id}`, `/{id}/status`; `PATCH`, `DELETE /{id}`; `POST /{id}/analyze`; `GET /{id}/findings`.
- **Jobs**: `GET /jobs/{id}`.
- **Conversations**: `POST`, `GET` list/detail, `PATCH`; `POST` and `GET /{id}/messages`; `GET /{id}/stream` (SSE).
- **Vendors**: CRUD, `POST /{id}/assess`, `GET /{id}/assessments`, `/assessments/latest`, `POST /vendors/compare`; `GET /vendor-comparisons/{id}`.
- **Health**: `GET /health/live`, `/health/ready`.
- Long work returns `202` with a job id; errors use `{error: {code, message, details, request_id}}`.

### Configuration
`CIQ_*` environment variables (validated at start-up), plus YAML in `config/`: `llm_routes.yaml` (model, effort, token and time budgets per route), `rag.yaml` (retrieval and evidence thresholds), `rules/`, `vendor/`, `clause_taxonomy.yaml`, `eval_tolerances.yaml`.

---

## 5. Model providers in V1

| Concern | Supported | Used in the Docker run |
|---|---|---|
| LLM | Anthropic, OpenAI (or any OpenAI-format server), fake | **OpenAI `gpt-4o-mini`** (`CIQ_OPENAI_SEND_EFFORT=false`) |
| Embeddings | OpenAI-compatible, Voyage, fake | **OpenAI `text-embedding-3-small`**, 1024 dimensions |
| Reranker | Cohere, local cross-encoder container, none, fake | **none** (stricter fallback gate) |

Provider errors are typed and never contain prompt text or credentials. OpenAI structured output uses strict JSON schema, with one fallback attempt.

---

## 6. Quality and verification

### Tests
- About **1,400 unit tests** (the count at the last full unit run), plus integration tests against real PostgreSQL, Redis and pgvector, a **17-case ownership matrix** over every route (a new unclassified route fails the test), property tests (Hypothesis), injection and canary tests, SSE reconnect tests, migration up/down/up, and Streamlit AppTest page tests. Lint (ruff) and strict mypy pass.
- **Opt-in e2e** (`tests/e2e`): real Celery and RabbitMQ (dead-lettering, replay, worker kill), UI flow, and `test_full_flow.py` against the running stack with real providers (9 steps, about 2.5 minutes).
- The mapping from risk to test is in `docs/test-matrix.md`.

### Evaluation
- Dataset `v1.0.0`: 73 chat questions, 12 contract-finding cases, 5 vendor cases, over four synthetic contracts for two vendors (Acme and Globex).
- The **offline regression gate** (real pipeline, fake providers) has a committed baseline (`eval/baselines/offline_v1.0.0.json`). Example values: citation validity 1.0, clause extraction accuracy 1.0, context recall 1.0. It guards against regressions; it says nothing about real-model quality.

### Verified in Docker with real OpenAI
Four synthetic contracts uploaded, extracted by Docling in the worker, chunked and embedded; a grounded answer with a validated citation over the live stream; a follow-up; a question the documents cannot answer (declined, no invented figure); history reload; a contract analysis (29 findings; for `acme_saas`, 16 of 23 rules matched the labelled status, and all 7 differences were `insufficient_evidence`, never a wrong risk assertion); both vendors assessed (Acme 10, Globex 22 and provisional); a comparison with narrative and the ranking suppressed; every UI page rendered with that data; no API key and no contract text in about 42,000 log lines.

---

## 7. Differences from the original plan (selected)

The full list is in `progress.md`. The ones worth knowing:
- RabbitMQ bootstrap uses `RABBITMQ_DEFAULT_*` instead of a `definitions.json` (a definitions file cannot read the password from the environment).
- Lost-worker recovery is done by the sweeper as well as by redelivery, because redelivery arrives before the heartbeat is stale.
- One processing job covers both ingestion stages (hand-off) instead of two jobs.
- Provider throttling uses task `rate_limit` plus backoff, not the Redis token bucket.
- Page numbers are nullable (Word files have none).
- `Container` lives in `api/container.py` and `WorkerContainer` in `workers/runtime.py`, not in `core`, because `core` may not import the layers it would wire.
- No CORS middleware (Streamlit calls the API server-side).
- The rate limiter fails open when Redis is unavailable.
- Chat does not use RabbitMQ.
- Citation validator additionally requires that numbers in a cited sentence appear in the source.

---

## 8. Known gaps and risks (honest list)

- **Quality on real contracts is unmeasured.** Tests and the Docker run used synthetic contracts. Extraction, clause boundaries and chunk sizes have not been checked on real agreements.
- **Evidence thresholds are uncalibrated.** They are the plan's initial values; real reranker and embedding score distributions have not been used to tune them. The Docker run had no reranker, so the stricter fallback gate was in effect and may decline answerable questions.
- **RAGAS full evaluation has never been run** (built; needs a model key and cost approval). The offline gate uses fakes.
- **Anthropic adapter has never called the real API.**
- **A prompt-injection limit**: a plain sentence containing a valid marker passes the citation check whatever it says, so an injected claim that the source passage itself states is judged grounded. The canary catches instruction leakage only.
- **No real browser pass** of the UI; the sign-out click path is untested; a page refresh signs the user out (the token is held in session state).
- **Cost and latency per operation are unmeasured.**
- **Integration tests are not in CI** and take about 30 minutes locally; flakiness is unknown (one unexplained flaky memory test was seen once).
- **Load and concurrency**: two large documents at once on a small Docker memory allotment were not tried.
- **Reranker** and **broker-delivered ingestion** through the Compose workers were verified only partly (broker drills date from M6).
- **`docker-compose.prod.yml` is a skeleton.**

---

## 9. Not started

- **M18, CI/CD**: pipeline for lint, type check, unit and integration tests, the slow Docling suite with cached models, a Compose smoke job, and image builds.
- **M19, final Docker deployment**: production Compose, secrets handling, TLS and proxy in front of SSE, backups.

---

## 10. Milestone history (commits on `main`, none pushed)

| Milestone | Commit |
|---|---|
| M1 skeleton, M2 Docker | `a5423cd` |
| M3 PostgreSQL | `5a18552` |
| M4 authentication | `22c779e` |
| M5 documents, M6 Celery, M7 extraction, M8 chunking and embeddings, M9 hybrid retrieval | `7d8411e` |
| M10 memory and conversations | `22f2e8a` |
| M11 chat graph, M12 contract analysis, M13 vendors | `72d8c56` |
| M14 evaluation | `e63ecfe` |
| M15 SSE streaming | `e111890` |
| M16 Streamlit UI | `6f12c09` |
| M17 integration testing | `21a9cdc` |
| OpenAI provider and `check-llm` | `f7b6e82` |
| OpenAI in Docker, full-flow e2e | `e29513d` |
| Interview guide | `ec09f24` |
| Sample contract for manual testing | `c19dbc9` |
| Per-package `logic.md` files | `a9d693f` |

## 11. Where to read more
`PROJECT_SPEC.md` (the plan) / `progress.md` (details, findings, deviations) / `docs/adr/` (27 decisions) / `docs/runbook.md` (operating it) / `docs/test-matrix.md` (risk to test) / `INTERVIEW_PREP.md` (teaching guide) / `src/covenantiq/*/logic.md` (per-package intuition) / `samples/complex_contract.md` (a long contract for manual testing).
