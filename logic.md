# CovenantIQ: what each part of the codebase does

One-page map. Each backend package also has its own `logic.md` with more detail (`src/covenantiq/<package>/logic.md`).

## How a request flows
```
Streamlit UI (frontend/) -> FastAPI (api) -> services -> db (PostgreSQL + pgvector)
                                   |            |-> Redis (cache, stream, locks)
                                   |            |-> RabbitMQ -> Celery workers -> services
                                   +-> chat graph (graphs) -> rag -> ai (LLM, embeddings, reranker) -> Redis Stream -> SSE
```
Layers only import downward (enforced by import-linter). `core` sits at the bottom and imports nothing else.

## Backend packages (`src/covenantiq/`)
| Package | What it does |
|---|---|
| `core` | Settings (`CIQ_*` env vars), enums, error types, logging, password and JWT security, Redis key names, ids. The shared foundation. |
| `db` | SQLAlchemy models, repositories, unit of work and the ownership scope. Every query is filtered to the owner. PostgreSQL is the source of truth. |
| `ingestion` | Turns an upload into searchable text: validation, storage, Docling extraction, clause (parent) and chunk (child) building, clause-type tagging and a prompt-injection scan. |
| `ai` | Adapters for the outside models behind small interfaces: LLM (OpenAI, Anthropic, fake), embeddings (OpenAI-compatible, Voyage, fake), reranker (Cohere, local cross-encoder, none, fake), plus the schemas the model must answer in. |
| `rag` | Retrieval and grounding: hybrid search (vector plus full-text), rank fusion, optional rerank, the evidence gate, context building, citation validation, abstention and caches. |
| `analysis` | Pure decision code with no model calls: contract rule statuses and severities, red-flag patterns, vendor signals and scoring. The model proposes; this code decides. |
| `graphs` | LangGraph workflows: chat, contract analysis, vendor assessment. Analysis graphs checkpoint to PostgreSQL so a crashed worker resumes. |
| `memory` | Conversation memory: a Redis window of recent turns plus a rolling summary, always rebuildable from PostgreSQL. |
| `streaming` | Live replies: typed SSE events, a Redis Stream buffer with resumable ids, and the one-reply-at-a-time lock. |
| `services` | The use cases: upload, ingestion, chat submit, analysis, vendors, jobs, sweeper, dead-letter replay, rate limiting. Own transactions and ownership checks. |
| `workers` | Celery app, queue topology on RabbitMQ, task base class (retries, dead-lettering), the per-process event loop and the task entry points. |
| `api` | FastAPI app: routers, request and response schemas, middleware, error handling, and the container that wires everything together. |
| `cli.py` | The `covenantiq` command: migrate, check-llm, check-embeddings, dlq list and replay, reembed, rerank-serve and others. |

## Other top-level folders
| Folder | What it does |
|---|---|
| `frontend/` | The Streamlit web UI (nine pages). It talks to the API over HTTP only and does not import the backend. |
| `config/` | YAML that tunes behaviour without code changes: LLM routes (model and budget per task), retrieval and evidence thresholds, the 30-rule contract rulebook, vendor signals and scoring weights, the clause taxonomy. |
| `migrations/` | Alembic database migrations (extensions, users and vendors, documents and chunks, conversations, analysis tables, nullable pages). |
| `eval/` | The evaluation harness: a labelled dataset of synthetic contracts and questions, an offline regression gate with a baseline, and a full RAGAS run (built, never run). |
| `tests/` | `unit` (fast, no services), `integration` (real PostgreSQL and Redis), `e2e` (opt-in, running stack and real providers), including an ownership matrix over every route. |
| `docker/`, `docker-compose*.yml` | Images (backend, frontend, PostgreSQL, Redis, RabbitMQ) and the Compose stack: postgres, redis, rabbitmq, migrate, api, two workers, optional reranker, streamlit. The prod file is still a skeleton. |
| `samples/` | A long fictional contract for manual testing. |
| `docs/` | 27 decision records (`adr/`), the runbook, the test matrix and evaluation reports. |

## Key ideas to remember
- **Modular monolith:** one codebase, several containers, strict layering.
- **PostgreSQL is the truth; Redis is only a cache.** Losing Redis costs speed, not correctness.
- **RabbitMQ is for background jobs only.** Chat runs as an asyncio task in the API and streams over Redis and SSE.
- **The LLM reads, code decides.** Scores, statuses and rankings are deterministic.
- **Answers are grounded or refused:** retrieved evidence must pass a gate, and every citation is checked against the source text.
- **Every query is scoped to its owner,** and a foreign id always returns 404.

## Other documents
`ProjectSpecV1.md` (what V1 is and its gaps) / `progress.md` (milestone log) / `INTERVIEW_PREP.md` (teaching guide) / `PROJECT_SPEC.md` (original plan).
