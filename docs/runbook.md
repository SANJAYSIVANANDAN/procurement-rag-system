# CovenantIQ operations runbook

Commands run inside the backend image, for example
`docker compose run --rm api covenantiq <command>`.

## Background jobs

Every document or analysis has a job row in PostgreSQL. **PostgreSQL is the truth**: the job row
always shows the final status before any message is dead-lettered. RabbitMQ only carries ids.

| Job status | Meaning |
|---|---|
| `queued` | Created and (normally) published, waiting for a worker |
| `running` | A worker claimed it. `heartbeat_at` is refreshed every 30 s |
| `succeeded` / `failed` | Final |

Two workers can never run the same job: the claim is a compare-and-set in the database, so a
duplicate delivery is acknowledged and ignored.

### A job is stuck in `queued`

The commit that creates a job and the publish that announces it are separate steps, so a crash
or broker outage between them leaves a job nobody will run. The API re-publishes jobs that have
been `queued` for more than 5 minutes (a background loop every `CIQ_SWEEP_INTERVAL_SECONDS`,
default 60, guarded by a Redis lock so only one replica sweeps). To do it by hand:

```bash
covenantiq sweep
```

Re-publishing is always safe. Each job is re-published at most once per 10 minutes.

### A job is stuck in `running`

A worker that dies (killed, out of memory, deployed over) leaves its job `running`. RabbitMQ
redelivers the message at once, but a heartbeat younger than 10 minutes still looks alive, so the
redelivered message is skipped and acknowledged: it cannot resume the job. The sweep covers this:
every run first puts `running` jobs whose `heartbeat_at` is older than 10 minutes back to `queued`
and publishes them again, so a job lost with its worker finishes about 10 to 11 minutes later.
`covenantiq sweep` does it by hand (it prints "another sweeper is already running" if the API's own
loop holds the lock, in which case the loop will do it).

A job that loses its worker 5 times is failed with `WORKER_LOST` instead of being tried again, so a
document that crashes the worker (out of memory, for example) cannot loop forever. Look at the
worker's memory limit and the document's page count before replaying it.

### Embedding

A document is ingested in two stages that share one job row. `ingest.process_document` extracts
and chunks it, stores the clauses and chunks, and hands the job on: the job goes back to `queued`
with `stage = embedding`, and `ingest.embed_document` (queue `embedding_generation`) embeds the
chunks. The job's stage, not the task name, decides which half runs, so either task name is safe.

- **Job `queued` at stage `embedding` for a long time:** the announcement was lost. The sweep
  re-publishes it as an embedding task; `covenantiq sweep` does it by hand.
- **`EMBEDDING_AUTH_FAILED`:** the provider rejected `VOYAGE_API_KEY`. Fix the key, then replay the
  dead letter (`covenantiq dlq replay embedding_generation`); only the missing vectors are sent.
- **`EMBEDDING_FAILED`:** more than 2 percent of the passages were refused by the provider. The
  refusals are in `document_chunks.metadata->>'embedding_error'`.
- **Provider outage:** the task retries with backoff and keeps what was already embedded.
- **Checking a provider before ingesting:** `docker compose exec api covenantiq check-embeddings`
  (after setting the provider variables in `.env` and running `docker compose up -d` so the
  containers pick them up). It prints the model, dimension and a similarity check, never a key.
- **Using OpenAI instead of Voyage:** in `.env` set `CIQ_OFFLINE=0`,
  `CIQ_EMBEDDING_PROVIDER=openai_compat`, `CIQ_EMBEDDING_MODEL=text-embedding-3-small`,
  `CIQ_EMBEDDING_DIM=1024`, `CIQ_OPENAI_COMPAT_BASE_URL=https://api.openai.com/v1`,
  `OPENAI_API_KEY=...` and `CIQ_RERANK_PROVIDER=none`. The adapter asks OpenAI for 1024 of the
  model's 1536 dimensions. Documents already embedded with another model must be re-embedded
  (next bullet). Contract text is sent to OpenAI.
- **Changing the embedding model:** set `CIQ_EMBEDDING_MODEL` (the dimension must match the column,
  1024), restart the workers, then run `covenantiq reembed` (`--document-id` for one document).
  Search over a document that is part-way through is not possible, so do it outside busy hours.

### Dead-lettered tasks

A task that cannot succeed (permanent error, retries exhausted, repeated timeout) first marks its
job `failed`, then is rejected without requeue, so RabbitMQ moves the message to
`<queue>.dlq`. Work queues: `document_processing`, `embedding_generation`,
`contract_analysis`, `vendor_analysis`.

```bash
covenantiq dlq list document_processing            # nothing is removed
covenantiq dlq replay document_processing           # all failed jobs in that DLQ
covenantiq dlq replay document_processing --job-id <uuid>
```

Replay resets each job from `failed` to `queued`, publishes the task again, and only then removes
the message from the DLQ. Messages whose job is not failed (already replayed) are left alone and
reported as skipped. The RabbitMQ management UI (dev: http://localhost:15672) shows the same
queues.

## Changing queue arguments

RabbitMQ refuses to redeclare a queue whose arguments differ (`PRECONDITION_FAILED`). Queues
created before the dead-letter arguments were added (Milestone 2 workers auto-created them without
any) must be deleted once:

```bash
docker compose down -v        # development: also drops the database volume
# or, keeping data: delete the four work queues in the management UI, then restart the stack
```

## Drill: killing a worker mid-task

1. Upload a PDF and wait until the job is `running`.
2. `docker compose kill worker-ingest`.
3. `docker compose up -d worker-ingest`. The redelivered message is skipped (the job looks alive
   for 10 minutes) and the job stays `running`.
4. After the heartbeat is more than 10 minutes old the sweep puts the job back to `queued` and
   publishes it; the worker claims it again (`attempt` goes up by one) and finishes it.

`tests/e2e/test_celery_rabbitmq.py::test_a_job_whose_worker_is_killed_mid_task_is_recovered_and_completes`
runs this against the real stack; it backdates the heartbeat instead of waiting 10 minutes.

## Checking the broker from the API

`GET /api/v1/health/ready` reports `database`, `migrations`, `redis` and `rabbitmq` separately.
Uploads fail fast with `503 DISPATCH_FAILED` when the broker is down (the document is marked
`failed`; uploading the same file again re-schedules it), and with `429 INGESTION_BACKLOG` when
`document_processing` holds more than `CIQ_MAX_INGEST_BACKLOG` messages.

## Conversation memory (Redis)

Each conversation has two Redis keys, both containing the user id and both expiring after
`CIQ_SESSION_TTL_SECONDS` (default 2 hours, refreshed on every read and write):

```text
ciq:<env>:sess:<user_id>:<conversation_id>:window   LIST  the recent turns, oldest first
ciq:<env>:sess:<user_id>:<conversation_id>:meta     HASH  last_seq, summary, summary_through_seq, window_tokens, schema_v
```

**Redis is a cache.** PostgreSQL holds every message and the rolling summary, so nothing in Redis
is precious: flushing it, restarting it, or letting keys expire or be evicted (`allkeys-lru`,
256 MB) only makes the next message in each conversation about 20 to 50 ms slower while the window
is rebuilt. Never "repair" a key by hand; delete it.

- **See what is cached** (never use `KEYS` on a busy instance; this uses `SCAN`):
  `docker compose exec redis sh -c 'redis-cli -a "$REDIS_PASSWORD" --scan --pattern "ciq:*:sess:*"'`
- **Force a rebuild of one conversation:** `DEL` its two keys. The next load rebuilds from PostgreSQL.
- **`memory.rebuilt` in the logs** says why a window was rebuilt: `miss` (expired or evicted),
  `last_seq_mismatch` (a reply reached PostgreSQL but was not recorded, for example the API died
  between the two), `schema_version`, `summary_out_of_step`, `corrupt_meta`, `corrupt_turn`,
  `out_of_order`, `token_count_mismatch`, or `expired_before_record`. A steady trickle of `miss` is
  normal; a burst of the others means something is writing to Redis that should not, or a deploy
  changed the stored format (bump `SCHEMA_VERSION` and it rebuilds on its own).
- **The window never grows past `CIQ_MEMORY_WINDOW_TOKENS` (3,000).** Older turns are folded into
  the conversation's summary (`CIQ_MEMORY_SUMMARY_TOKENS`, 600) by a background task:
  `memory.summarised` on success, `memory.summary_failed` when the model was unavailable. A failed
  summary deletes the session so the next load retries it; if `memory.summary_failed` repeats, check
  the model provider, not Redis.
- **Another user's conversation** is a 404 before Redis is consulted, so a wrong id never creates a
  key.

## Searching and tuning retrieval

`covenantiq search` runs the retrieval engine and prints what it did. It prints passage text, so run
it where you are allowed to read the contracts.

```bash
docker compose exec api covenantiq search "What is the cap on the supplier's liability?"     --user alice@example.com            # all of that user's ready documents
    # --doc <uuid>   only this document (repeatable)    --no-rerank   skip the reranker
    # --k-final 8    passages kept                      --json        the full trace
```

What the output tells you:

- **keywords / full-text query:** what the lexical half searched for. If a term you expected is
  missing it was a stop word (see `STOP_WORDS` in `rag/query_analysis.py`); put an exact phrase in
  quotes to keep it whole.
- **dense N, lexical N:** how many candidates each search found. `dense 0` with a `note:` means no
  chunk is embedded with the configured model (change of model: run `covenantiq reembed`) or the
  embedding provider was down; `lexical 0` means none of the words occur.
- **Per passage:** `dense#` and `lexical#` are the ranks in each search (`-` = not found by that
  search), `rrf` the fused score, `rerank` the reranker's score. A right answer found by only one
  search is the case hybrid search exists for.
- **reranking skipped: ...:** the reason (switched off, no reranker, timeout, error). The evidence
  verdict is then at most `moderate`.
- **evidence:** `strong` / `moderate` answer (moderate states its limits), `weak` / `none` abstain
  with a template and no model call. The reasons say which rule decided.
- **timings:** if `embed` or `rerank` dominates, see the reranker and embedding sections. A dead
  Redis costs only a few 250 ms timeouts, then the cache is skipped for 30 seconds.

Tuning lives in `config/rag.yaml` (candidate counts, the RRF constant, the clause-type boost, the
evidence and citation thresholds); `CIQ_CONTEXT_MAX_TOKENS`, `CIQ_PARENT_MAX_TOKENS` and
`CIQ_EXACT_SCAN_THRESHOLD` override the matching values. The numbers are initial values: calibrate
them on a labelled question set (milestone 14) rather than by feel, and recalibrate the rerank-score
thresholds whenever the reranker changes.

## Reranker with Cohere (hosted)

The simplest reranker to run: nothing extra to start, and it fits the 3 second budget (ADR-018).
Contract text (the query and the candidate passages) is sent to Cohere.

```bash
# .env (put the key here yourself, never in chat or a commit):
#   CIQ_RERANK_PROVIDER=cohere
#   COHERE_API_KEY=...
#   CIQ_RERANK_MODEL=rerank-v5.0-fast      # or rerank-v5.0-pro for the best quality
docker compose up -d                          # picks the new variables up
docker compose exec api covenantiq check-rerank
```

`check-rerank` sends four generic sentences and expects the liability clause first.

- **`COHERE_API_KEY is required`:** the variable is empty or unset in the container; Compose passes
  it through from `.env`.
- **`rerank failed: Cohere rejected the API key`:** wrong key, or the key lacks rerank access. The
  caller keeps the fused order meanwhile, and after three failures it stops calling for 30 seconds.
- **HTTP 429:** a rate limit (trial keys are limited). Same fallback; use a production key.
- **Not a Voyage key:** `CIQ_RERANK_PROVIDER=voyage` is not implemented yet and fails at start-up.
- **Which model:** `rerank-v5.0-fast` for latency, `rerank-v5.0-pro` for quality. Changing it needs
  no migration; reranking has no stored state. The Ettin container is not needed (and is not
  started) with this provider.

## Reranker

Reranking with `cross-encoder/ettin-reranker-150m-v1` runs in its own container, never inside the
API or a worker (ADR-017). It is behind a Compose profile so the default stack stays small.

```bash
# .env:  CIQ_RERANK_PROVIDER=cross_encoder   (CIQ_OFFLINE=0)
docker compose --profile rerank up -d        # also builds the image on first use
docker compose exec api covenantiq check-rerank
```

`check-rerank` ranks four generic sentences and prints the order and the time. The service logs
`rerank.model_loaded` once (the model is baked into the image, so no download happens) and
`rerank.scored` with counts and milliseconds for every request; it never logs the text.

- **CPU.** Two settings, kept equal: `CIQ_RERANK_CPUS` caps the container (Docker's CPU quota; a
  ceiling, not a reservation, so the workers still share the host's cores) and `CIQ_RERANK_THREADS`
  is how many threads PyTorch spreads the model over. Threads above the cap are throttled; a cap
  above the threads leaves cores idle. Change them in `.env`, then
  `docker compose --profile rerank up -d --force-recreate reranker`. Measured in the container on
  a 16-CPU host for 30 chunks of 350 tokens: 4 CPUs 20.8 s, **8 CPUs 11.9 s (default)**, 12 CPUs
  10.2 s, 16 CPUs 19.7 s. More than about 8 gives little, and giving the reranker every core makes
  it slower (the Docker VM, the workers and the host need some). Check what Docker applied with
  `docker inspect covenantiq-reranker-1 --format '{{.HostConfig.NanoCpus}}'` (8 CPUs = 8000000000).
- **Speed.** On CPU the 150M model scores about 2.5 pairs per second for 350-token chunks with 8
  CPUs (1.4 with 4), so 30 candidates take roughly 12 seconds. A caller waits at most
  `CIQ_RERANK_TIMEOUT_SECONDS` (default 3) and then keeps the fused order, so on CPU reranking is
  usually skipped. Options: run the reranker on a GPU host (set `CIQ_RERANK_URL`), give it more
  CPU (`CIQ_RERANK_CPUS`, `CIQ_RERANK_THREADS`), send fewer candidates, or bake in a smaller Ettin
  reranker (68M, 32M or 17M).
- **`rerank.deadline_exceeded` in the logs:** requests the server abandoned because the caller's
  time ran out; frequent ones mean the hardware is too slow for the candidate count. After three
  in a row the caller stops asking for 30 seconds (`rerank.skipping`).
- **503 `busy`:** more than three requests were in flight or queued. The caller keeps the fused
  order. Add a second reranker container behind a load balancer if this is constant.
- **The reranker is down:** nothing else is affected. Retrieval keeps the fused order and marks
  `rerank_skipped`.
- **Memory:** the container is limited to 2 GB (the model uses about 1 GB).

## Running next to a local PostgreSQL, Redis or RabbitMQ

The dev override publishes 5432, 6379, 5672 and 15672 on the host. If a Windows or Linux service
already listens on one of them (a local PostgreSQL on 5432 is common), host-side tools connect to
that service instead of the container, and fail with "password authentication failed" for a
password that is correct. Run operator commands inside the container instead:

```bash
docker compose exec api covenantiq sweep
docker compose exec api covenantiq dlq list document_processing
```

or publish the container on another port. Inside the Compose network the services always use
their own hostnames, so only host-side tools are affected.
