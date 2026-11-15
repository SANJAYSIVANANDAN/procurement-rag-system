# services: the use cases (business orchestration)

## What it is
The middle layer. Each service implements a use case ("upload a document", "submit a chat message", "assess a vendor") by coordinating repositories, Redis, the task queue and the graphs. Routers and Celery tasks both call services; nothing above them touches SQL.

## Intuition
Services are where **transactions, ownership and ordering** are decided. Every public method starts with a `Principal` (the caller), checks ownership in PostgreSQL first, and only then touches Redis, RabbitMQ or a model. Each persistence call is its own `UnitOfWork`, so there are no long-lived sessions held across slow model calls.

## Files by area
**Accounts and safety**
- `auth_service.py`: register (argon2, password policy, normalised email), login (JWT), authenticate.
- `rate_limiter.py`: fixed-window counters in Redis per bucket and identifier; `RateLimited` carries retry-after.
- `health_service.py`: readiness checks for PostgreSQL, migrations, Redis, RabbitMQ.

**Documents and ingestion**
- `document_service.py`: streaming upload (size enforced as it arrives), validation, hashing, duplicate detection, creates the document and job, enqueues; list, get, status, patch, delete.
- `ingestion_service.py`: the worker-side pipeline (extract, chunk, tag, scan, embed, persist) with stage progress, resumable embedding and `purge`.
- `reembed_service.py`: re-embed documents after an embedding-model change.

**Jobs and reliability**
- `job_service.py`: the job state machine: `claim` (CAS), `touch` heartbeat, `hand_off`, `release`, `mark_succeeded/failed`, `reap_lost` (takeover of stale heartbeats, max 5 attempts), `stale_queued`.
- `task_dispatcher.py`: publishes Celery tasks with a publish retry policy; `QueueDepthProbe` caches queue depth for backpressure (`IngestionBacklog`); `NullTaskDispatcher` for tests.
- `sweep_service.py`: every 60 s, under a Redis lock, re-dispatches jobs stuck as queued (the commit-then-publish gap), with a guard so one is not re-sent repeatedly.
- `dlq_service.py`: inspect and replay dead-lettered messages (`covenantiq dlq list/replay`).

**Chat**
- `conversation_service.py`: conversations, scoping to documents, history, storing a user message plus a pending assistant message in one transaction.
- `chat_service.py`: `submit` = verify ownership, answer an idempotent replay, take the per-conversation Redis lock, store the question, start the graph as a background task. The task always releases the lock.
- `chat_persistence.py`: `ChatPersistencePort` implementation the graph uses.
- `stream_service.py`: checks the listener owns the conversation and the message is an assistant message, then opens the stream (404 otherwise).

**Analysis**
- `contract_analysis_service.py`: start (create job, enqueue), run on a worker, read findings.
- `vendor_assessment_service.py`, `vendor_comparison_service.py`: check preconditions (e.g. latest assessments exist and are config-compatible), queue, run, read.
- `vendor_service.py`: vendor CRUD. `analysis_store.py`, `vendor_store.py`: the graphs' storage ports on PostgreSQL.

**Support**
- `dto.py`: immutable result objects returned to routers (never ORM entities). `pagination.py`: cursor helpers.

## Key ideas
- **Ownership first, Redis second**: a wrong id is a 404 before any cache is reached.
- **Idempotency**: duplicate uploads return the existing document; resubmitting a chat message with the same key replays instead of regenerating.
- **Commit, then publish**, with a sweeper to close the gap, instead of distributed transactions.
- **Graphs reach the DB only through ports defined here.**

## Interview soundbite
"Routers and tasks are both thin callers of the same services, so behaviour is identical whether a request is sync or background. Services own the transaction boundaries and the ownership checks."
