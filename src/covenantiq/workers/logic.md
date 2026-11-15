# workers: Celery tasks, queues and process runtime

## What it is
The background-processing side: the Celery app, queue topology on RabbitMQ, the task base class, the long-lived per-process runtime and the task entry points. Heavy, slow or retryable work runs here, not in the API.

## Intuition
A worker task is a **thin, synchronous wrapper**: it receives a job id, bridges into async code and calls a service. The queue message is only a *nudge*. The job row in PostgreSQL is the truth, and the service claims it with a compare-and-set, so duplicate or late deliveries are harmless.

## Files
| File | What it does |
|---|---|
| `celery_app.py`, `app_factory.py` | Build the Celery app from settings. `publisher_only=True` is used by the API, which only sends tasks. |
| `queues.py` | Topology: one **direct exchange** `covenantiq`, four work queues (`document_processing`, `embedding_generation`, `contract_analysis`, `vendor_analysis`), and a dead-letter exchange `covenantiq.dlx` routing each queue's rejects to `{queue}.dlq`. |
| `base_task.py` | `CovenantTask`: `acks_late`, `reject_on_worker_lost`, prefetch 1. Transient errors retry with backoff (and a soft-timeout retry); permanent errors are rejected so the message goes to the DLQ. |
| `runtime.py` | Per-child runtime (prefork). Each child owns **one long-lived event loop** and one container built on it (NullPool DB engine). Tasks call `run_async(coro)`. This avoids creating a new loop per task, which would break pooled connections. Uses the selector loop on Windows. |
| `heartbeat.py` | While a job runs, touches its `heartbeat_at` every 30 s. Another worker can take over a job whose heartbeat is older than 10 minutes. |
| `signals.py` | Worker lifecycle hooks: configure logging, init and shut down the runtime, declare the DLQ topology. |
| `tasks/ingestion.py` | `process_document`, `embed_document`, `purge_document`. |
| `tasks/analysis.py` | Contract analysis, vendor assessment and vendor comparison tasks. |

## How a document moves
```
API upload -> job row (queued) -> publish process_document to RabbitMQ
worker: claim (CAS) -> extract/chunk -> hand_off to embedding stage -> publish embed_document
worker: embed -> document ready
```
If a worker dies mid-task, `reject_on_worker_lost` returns the message, the heartbeat expires, and another worker claims the job and resumes (embedding resumes without duplicates; analysis resumes from its checkpoint).

## Key ideas
- **Two workers pools**: `worker-ingest` (Docling, memory-heavy) and `worker-analysis` (LLM-bound) scale independently.
- **Idempotency** comes from the job state machine, not from the broker.
- **Why RabbitMQ**: routing, acks, per-queue dead-lettering and visibility into depth, which suits durable background jobs. It is *not* used for chat.
- **Gap sweeper**: publishing happens after the DB commit (two steps). If the process dies between them, `services/sweep_service.py` re-dispatches stale queued jobs.

## Interview soundbite
"Tasks only carry an id. Correctness comes from PostgreSQL: a compare-and-set claim, a heartbeat for takeover, and a sweeper for the commit-then-publish gap. RabbitMQ gives delivery and dead-lettering; it does not decide state."
