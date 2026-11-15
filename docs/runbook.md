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

## Chat replies

`POST /api/v1/conversations/{id}/messages` stores the question and an empty reply, answers `202`
with `{user_message_id, assistant_message_id, stream_url, status}`, and writes the reply in a
background task. Follow it live at `stream_url` (below), or read the finished reply from
`GET /conversations/{id}/messages` (`status` is `completed`, `failed` or `interrupted`).

| You see | It means |
|---|---|
| `409 GENERATION_IN_PROGRESS` | A reply is already being written in that conversation. Wait for it. |
| `503 DEPENDENCY_UNAVAILABLE` on submit | Redis is unreachable (the lock needs it). Nothing was stored. |
| `200` instead of `202` | The same `client_message_id` was sent before: the original ids come back and nothing is generated. |
| Message `failed` with partial text | The model failed after it began streaming (not retried, by design). The user asks again. |
| Message `interrupted` | The API restarted mid-reply, or the sweeper found it abandoned (`chat.stale_messages_interrupted`). |
| Message `completed`, grounding `abstained` | Not enough in the documents; no model call was made to answer. This is the intended behaviour, not a fault. |

- **One reply at a time per conversation** is a Redis key `ciq:<env>:lock:conv:<conversation_id>`
  holding a random token (120 s, extended every 30 s while the reply is written). If a conversation
  seems stuck on `409` with nothing generating, the key expires by itself within two minutes;
  deleting it by hand is safe only if no reply is running.
- **Logs to know:** `chat.run_finished` (with `total_ms`), `chat.node_failed` (node and error type,
  never the prompt), `chat.classify_fallback` / `chat.rewrite_fallback` (the routing model failed
  and the question was searched as written), `chat.memory_record_failed` (the reply is stored; the
  session was dropped and rebuilds), `llm.retry` (a transient model failure before the first token).
- **Model configuration** is `CIQ_LLM_PROVIDER`, `CIQ_LLM_MODEL_DEFAULT` and `config/llm_routes.yaml`
  (effort, token and time budget per route; a route may name another model). A misspelt route name
  or an invalid value stops the API at start-up. **Using OpenAI for the model:** `CIQ_LLM_PROVIDER=openai`,
  `CIQ_LLM_MODEL_DEFAULT=<a model you have access to>` and `OPENAI_API_KEY` (the key and
  `CIQ_OPENAI_COMPAT_BASE_URL` are shared with the OpenAI embeddings). Routes can name other models in
  `config/llm_routes.yaml`; `effort` is sent as `reasoning_effort` (`xhigh` and `max` become `high`),
  so for a model without that parameter set `CIQ_OPENAI_SEND_EFFORT=false`. `covenantiq check-llm` makes
  one tiny streamed and one structured call and prints the model and token counts (never the key).
  Contract text is sent to OpenAI. Answers and analyses were designed and measured against Anthropic's
  models; with OpenAI the prompts are the same, but their quality and cost are unmeasured. `ANTHROPIC_API_KEY` is required unless the provider
  is `fake`.
- **Shutdown** waits up to 20 s for replies in flight, then marks the rest `interrupted`. A deploy
  that kills the process outright is covered by the sweeper (every minute, replies older than
  10 minutes).
- **`covenantiq migrate`** now also creates the LangGraph checkpoint tables (`checkpoints`,
  `checkpoint_writes`, `checkpoint_blobs`, `checkpoint_migrations`). It is safe to run repeatedly.

## Streaming a reply (SSE)

`GET /api/v1/conversations/{id}/stream?message_id=<assistant_message_id>` (bearer token,
`Accept: text/event-stream`). Try it: `curl -N -H "Authorization: Bearer $TOKEN" "$URL"`.

- **Events**, in this order: `message_start`, any `status` and `token`, any `citation`, then exactly one
  `message_complete` (its `content` is the validated final text: replace the draft with it) or
  `error`. Every frame has an `id`. A comment ping every 15 s keeps proxies from closing the
  connection; `X-Accel-Buffering: no` stops nginx buffering it.
- **Resuming:** reconnect with `Last-Event-ID: <last id>` and the rest arrives, with no duplicates
  and no gaps. Without it the stream starts again from `message_start`. Reconnecting after the
  terminal event returns nothing.
- **Closing the tab never stops a reply.** It finishes in PostgreSQL regardless.
- **Where events live:** a Redis Stream `ciq:<env>:stream:<message_id>` (about 5,000 entries, expires 10
  minutes after the reply ends; one hour after it started if the producer died). Token events are
  merged (every 30 ms or 64 characters), so a token event holds a few words. After expiry, or if
  Redis lost the events, a finished reply is replayed from PostgreSQL as `message_start`, one `token`
  with the whole text, the citations and the terminal event (ids `0-1`, `0-2`, ...).

| You see | It means |
|---|---|
| `error` with `STREAM_STALLED` | Nothing arrived for `CIQ_STREAM_IDLE_TIMEOUT` (90 s) and the reply is not final: the producer died or is stuck. The sweeper marks it `interrupted` within about 10 minutes (`chat.stale_messages_interrupted`). The client may ask again. |
| `error` with `INTERRUPTED` | The API restarted mid-reply, or the sweeper gave up on it. |
| `error` with `GENERATION_FAILED` | The reply failed (replayed from the database); partial text, if any, is in the preceding `token`. |
| `404` | Not your conversation, or the message is not an answer in that conversation. Existence is never revealed. |
| Stream works but arrives all at once | A proxy is buffering. Disable response buffering for this path. |
| `stream.publish_failed` in the API log | Redis refused an event. The reply is unaffected and finishes in PostgreSQL; a listener is served from there. |

- The listener uses its own Redis connection (`stream_redis`) because it waits up to 15 s inside
  `XREAD BLOCK`, longer than the 2 s timeout the other Redis clients use.

## The web UI (Streamlit)

`docker compose up` starts it at http://localhost:8501 (service `streamlit`, image `covenantiq-frontend`). It
reads one setting, `CIQ_API_BASE_URL` (Compose sets `http://api:8000/api/v1`), and talks to nothing else.
Without Docker: `uv run streamlit run frontend/app.py` (needs the API running; the default URL is
`http://localhost:8000/api/v1`).

| You see | It means |
|---|---|
| "The CovenantIQ API cannot be reached right now." | `CIQ_API_BASE_URL` is wrong, or the API is down or still starting. Check `GET /api/v1/health/ready`. |
| Back at the sign-in page with "Your session ended" | The token expired (`CIQ_JWT_TTL_MINUTES`). Sign in again; conversations and documents are unchanged. |
| An error with a "Reference: ..." line | Quote the reference: it is the API's request id, searchable in the API log (`request_id`). |
| Chat shows "Working on it..." for a long time, then "The connection was lost" | The stream dropped three times in a row. A proxy may be buffering or closing it (see "Streaming a reply"). Regenerate; the answer may already be in the history. |
| The dashboard numbers look old | They are cached for 30 seconds; a document's status refreshes on the next reload. |

- **A refresh signs you out** (the token lives in the browser session only). Your conversations are kept.
- **Uploads** show a progress bar per file, polled every 2 seconds. Leaving the page does not stop processing.
- **Analysis and assessment** show a progress bar while the job runs; leaving the page does not stop the job, and the result is there when you come back.
- **Tests:** `uv run pytest tests/unit/frontend` needs no server. With an API running,
  `CIQ_E2E_UI=1 CIQ_API_BASE_URL=http://localhost:8000/api/v1 uv run pytest tests/e2e/test_ui_flow.py` renders every page against it.

## Log hygiene

- Logs hold identifiers, counts, timings and error types: never a question, a passage, a prompt or an
  answer, and tracebacks carry no local variable values (`show_locals` is off in both formats). Do not
  add document or user text to a log call or an exception message.
- `chat.canary_leaked` (warning): a model reply contained the confidential reference that every answer
  prompt carries, which means it was talked into revealing its instructions. The user saw a refusal.
  Look at which document the passages came from (`chat.run_finished` has the message id) and whether
  it carries an injection warning (`injection_flagged_blocks` in the document's extraction quality).

## Contract analysis

`POST /documents/{id}/analyze` queues a job on the `contract_analysis` queue (consumed by the
`analysis` worker); `GET /jobs/{id}` shows its stage and progress; `GET /documents/{id}/findings`
returns the findings of the latest succeeded analysis (`?job_id=` for another, `?severity=`,
`?status=`, `?category=` to filter; the summary always counts the whole analysis).

| You see | It means |
|---|---|
| Job `failed`, `ANALYSIS_DEGRADED` | More than half the rules could not be assessed (the model or retrieval was down). Nothing was stored. Fix the cause, then `covenantiq dlq replay` or analyse again with `force`. |
| Job `failed`, `DOCUMENT_NOT_READY` | The document was not `ready` (or was deleted) when the worker started. |
| Finding `insufficient_evidence` with `details.error` | That rule's batch failed (`LLM_UNAVAILABLE`, `RETRIEVAL_FAILED`, ...). Re-run with `force` once the cause is fixed. |
| Finding `insufficient_evidence` with `details.reasons` | The system could not decide: poor extraction, an unsure model, an explanation the passages do not support, or an absence it could not prove. This is intended, not a fault. |
| `notes` on the findings response | The contract type was not set, or an extra pass (obligations, a contradiction check) was skipped. |
| `200` from `/analyze` | An analysis is already running, or a current one exists for this ruleset and document. `force: true` runs it again unless one is running. |

- **A killed worker resumes.** The graph checkpoints to PostgreSQL after every step (tables created by
  `covenantiq migrate`). The redelivered task continues the same thread, and batches that already
  finished are not paid for again; `analysis.resuming` in the log says so. A finished job's checkpoints
  are deleted. If you must discard a stuck thread, delete its rows by `thread_id` (the job id) in
  `checkpoints`, `checkpoint_writes` and `checkpoint_blobs`; the next run starts over.
- **Rules** live in `config/rules/contract_baseline_v1.yaml` (25 baseline rules and the unusual
  provisions). The file is validated at start-up: an invalid rule stops the API and the worker with
  the rule's id in the message. Changing a rule's meaning means a new `semver`; the version is stored on
  every job, so a finding says which ruleset produced it.
- **Tuning** is in the `analysis:` section of `config/rag.yaml`: `absent_threshold` (best rerank score
  below which "absent" is considered), `pages_with_text_ratio` (extraction quality needed to claim
  absence), `rules_per_batch`, `max_degraded_fraction`. Concurrency is `CIQ_ANALYSIS_MAX_CONCURRENCY`
  (default 3); raise it only if the model provider's rate limit allows.
- **Logs to know:** `analysis.finished` (findings count), `analysis.batch_failed`,
  `analysis.retrieval_failed` (a rule degraded), `analysis.obligations_failed`,
  `analysis.node_failed` (a node failed in a way no rule result describes; the job fails).
- Every findings response carries the disclaimer: automated analysis for decision support, not legal
  advice.

## Vendor assessment and comparison

`POST /vendors/{id}/assess` starts any contract analysis the vendor's ready documents still need,
then queues the assessment behind them (`pending_dependencies` in the response). Poll the job at
`GET /jobs/{id}`, then read `GET /vendors/{id}/assessments/latest`. `POST /vendors/compare` compares
two to five assessed vendors; read the result at `GET /vendor-comparisons/{id}` (`result` is empty
until the job has succeeded).

| You see | It means |
|---|---|
| Assessment job `queued` for a while | It is waiting for a contract analysis it depends on. The worker re-checks every 30 s, up to 20 times, without holding a slot. |
| Job `failed`, `DEPENDENCY_NOT_READY` | A contract analysis did not finish within about ten minutes, or a document has none. Check the contract analysis jobs, then assess again. |
| Job `failed`, `NO_DOCUMENTS` or `VENDOR_NOT_FOUND` | The vendor has no ready documents, or was deleted. |
| `provisional: true` on an assessment | Less than 60% of the weighted evidence, or fewer than four dimensions assessed. The score is shown but not rankable. |
| A dimension `insufficient_evidence` or `not_assessed` | Too few of its signals had evidence (`missing_information` lists them and why). Not a fault: the system says what it could not tell. |
| `notes` on an assessment | A contract analysis failed, an extraction or explanation fell back, or observations were downgraded. |
| Compare `409 ASSESSMENT_REQUIRED` | A vendor has no completed assessment (`details.vendor_ids`). |
| Compare `409 INCOMPATIBLE_ASSESSMENTS` | The assessments used different scoring configurations, or an older one than the current. Re-assess. |
| Compare `409 CONFLICT` | An assessment is older than the vendor's documents or analyses. Re-assess, or send `require_fresh: false`. |
| `ranking: []` with a reason | A vendor is provisional, or fewer than four dimensions are comparable. By design: no ranking is better than a misleading one. |
| `narrative_source: "template"` | The model's summary failed validation (or the model was down); the deterministic summary was used. `result.notes` says why. |

- **Configuration** is `config/vendor/signals_v1.yaml` (26 signals, their weights and sources) and
  `config/vendor/scoring_v1.yaml` (dimension weights, risk values, the three absences that count as
  risk, coverage thresholds, severity bands). Both are validated at start-up; an invalid file stops the
  API and the worker. The scoring `version` is stamped on every assessment: change it whenever a
  number changes, and assessments made before are then (correctly) not comparable with new ones.
- **Rescoring never needs the model.** The score is a pure function of the stored signals; to see the
  effect of a configuration change on past data, re-assess.
- **A killed worker resumes** like a contract analysis: checkpoints per job in PostgreSQL, finished
  steps are not repeated (`analysis.resuming` in the log).
- **Logs to know:** `vendor.waiting` (a dependency is still running), `vendor.assessed`,
  `vendor.extraction_failed`, `vendor.retrieval_failed`, `comparison.narrative_failed`.
- Findings and assessments carry the disclaimer: automated analysis for decision support, not legal
  advice.

## Evaluation

Two runs, both from `eval/` (the design is in `PROJECT_SPEC.md` §28 and `docs/adr/ADR-024.md`).

| | Offline | Full |
|---|---|---|
| Command | `make eval-offline` | `make eval-full` |
| Providers | hash embeddings, lexical reranker, scripted model | the configured real ones, plus RAGAS |
| Cost | free, about 90 s | paid, needs keys and `uv sync --group eval` |
| Where | every CI run (`eval-offline` job) | `eval.yml` (manual and nightly), not required to merge |
| Gate | fails on regression against `eval/baselines/offline_v1.0.0.json` | report only |

- **Offline needs a migrated PostgreSQL with pgvector.** Set `CIQ_EVAL_DATABASE_URL` (it also falls
  back to `CIQ_TEST_DATABASE_URL`, then `CIQ_DATABASE_URL`). The run creates its own user, ingests the
  twelve contracts, and deletes everything it made, so it can share a database with other work; do not
  point it at a production database. Run `covenantiq migrate` first or it will say so.
- **A failure of the gate** prints a table of metric, baseline, current, change and allowed.
  `regressed` means worse than the tolerance in `config/eval_tolerances.yaml`; `missing` means a metric
  stopped being reported. If the change is intended (for example a retrieval improvement that moves
  other numbers), update the baseline deliberately: `make eval-baseline NOTE="why"`, then commit it
  with that reason in the message. Never edit the JSON by hand.
- **The dataset is protected by checksums.** If the run refuses with "does not match its manifest",
  a dataset file changed. To change the dataset on purpose, edit `eval/datasets/generate_v1.py`
  (or add a new version, since an edit to an existing version breaks every baseline made on it), run
  `make eval-dataset`, and commit the regenerated files; the baseline must then be rewritten.
- **Reports** go to `eval/reports/` (ignored by git; CI uploads them as artifacts) as JSON with every
  metric, its 95% interval and per-example results, plus a Markdown summary. They record the git
  revision, dataset version and hash, prompt hashes, models per route, and the ruleset and scoring
  versions.
- **Reading the offline numbers:** they measure the deterministic logic under fake providers, not
  quality. See `docs/eval/offline_v1.0.0.md` before quoting any of them. The full run's report has not
  been made; targets in spec 28.7 are targets until it is.
- **The full run asks before spending** and refuses fake providers. It writes the same report plus the
  RAGAS metrics, and prints each spec target next to the measured value.
- `python -m eval.scripts.build_dataset` shows what the real chunker makes of each contract (chunk
  ids, pages, clause numbers); it cleans up after itself.

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
