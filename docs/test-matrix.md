# Critical test cases (spec 34.3): where each one lives

Every row of the spec's list, the tests that cover it, and what is not covered. "Opt-in" tests need
a running service or a key and are skipped otherwise; everything else runs in `uv run pytest`.
Paths are relative to `tests/`.

| Area | Tests | Not covered |
|---|---|---|
| Ownership | `integration/api/test_ownership_matrix.py`: user B gets 404 on every endpoint that takes one of A's ids (17 cases, including the stream), 401 without a token, lists show only your own, cross-references (vendor on an upload, document in a conversation) are refused or dropped; **a new route that is not classified there fails the test**. Per-feature 404 tests in `test_documents`, `test_conversations`, `test_chat_api`, `test_chat_stream`, `test_vendors`, `test_analysis_api`, `test_vendor_analysis_api`, `test_jobs` | A finished comparison is not seeded in the matrix; its ownership is asserted in `test_vendor_analysis_api` |
| Ownership (repo) | `unit/db/test_scope_contract.py` (every user-data repository method takes a scope) | |
| Upload | `integration/api/test_documents.py` (type mismatch, encrypted PDF, 413 mid-stream, too many pages, macro DOCX, duplicate returns the existing), `unit/ingestion/test_validation.py` (zip bomb) | |
| Ingestion idempotency | `integration/api/test_worker_flow.py` (duplicate delivery), `integration/db/test_embedding_stage.py` (a retry resumes without duplicates), `integration/db/test_ingestion_service.py` | A kill in the middle of embedding through a real broker: `e2e/test_celery_rabbitmq.py` (opt-in) kills a worker mid-task |
| Chunking | `unit/ingestion/test_chunking.py` | |
| Retrieval | `integration/db/test_retrieval_engine.py`, `test_chunk_search.py` (filters against identical text of another user) | |
| Evidence gate | `unit/rag/test_evidence.py`, `unit/graphs/test_chat_graph.py` (abstention) | Thresholds are the spec's defaults, not tuned (needs real providers) |
| Citation validator | `unit/rag/test_citations.py` | |
| Prompt injection | `unit/ingestion/test_injection_scan.py` (flagging), `unit/graphs/test_chat_injection.py` (the model sees a warning attribute and escaped text, the prompt says sources are data, **a reply containing the per-request canary is replaced by a refusal**) | A real-model variant (`CIQ_E2E_LLM`, evaluation). A plain sentence with a valid marker passes the citation check whatever it says, so an injected claim that the passage itself states is judged grounded; the canary only catches leaking the instructions |
| Chat persistence | `integration/chat/test_chat_service.py`, `integration/api/test_chat_api.py` (seq, citations, idempotent resubmit, 409) | |
| SSE | `integration/api/test_chat_stream.py` (order, reconnect after every frame, disconnect, replay after expiry, stalled producer), `unit/streaming/` (coalescing, ordering property) | A proxy in front, a real model |
| Memory | `integration/memory/`, `unit/memory/`, `integration/chat/test_chat_flows.py` (Redis flushed in the middle of a conversation: the follow-up still sees the earlier turn and the window comes back) | |
| Contract analysis | `unit/graphs/test_contract_graph.py` (statuses, a failed or refused batch degrades only its rules, resume without repeating calls), `integration/analysis/test_contract_analysis.py` (a real checkpoint resume) | |
| Vendor scoring | `unit/analysis/test_vendor_scoring.py` (properties, absence overrides), `unit/analysis/test_vendor_config.py` | |
| Comparison | `unit/analysis/test_vendor_comparison.py`, `integration/vendor/test_vendor_comparison_service.py` (not comparable, incompatible versions 409, ranking suppressed) | |
| Celery | `unit/workers/test_task_base.py`, `unit/services/test_dlq_service.py`, `integration/api/test_worker_flow.py`; `e2e/test_celery_rabbitmq.py` (opt-in: dead-lettering, replay, worker kill) | Broker behaviour has not been run since M6 |
| Logging | `unit/core/test_logging.py` (redaction, **a traceback shows no local variables**, in both formats), `integration/chat/test_chat_flows.py` (neither a contract passage nor the question appears in the logs of a validated reply or a failed one, with the application's log configuration) | Ingestion, analysis and vendor runs are not scanned |
| Migrations | `integration/db/test_migrations.py` (upgrade, downgrade, upgrade) | |

## Failure injection

| Failure | Where |
|---|---|
| Embedding provider 429 | `unit/ai/test_embeddings.py` (retry with backoff), `integration/db/test_embedding_stage.py` (resume) |
| Reranker timeout or error | `integration/db/test_retrieval_engine.py` (fused order kept, `rerank_skipped`) |
| LLM refusal | `unit/graphs/test_chat_graph.py` (chat), `unit/graphs/test_contract_graph.py` (analysis), `unit/ai/test_llm_retry.py` |
| Redis flushed mid-conversation | `integration/chat/test_chat_flows.py` |
| Worker killed mid-analysis | `integration/analysis/test_contract_analysis.py`, `integration/vendor/test_vendor_assessment.py` (real checkpoint resume); `e2e/test_celery_rabbitmq.py` (opt-in, a real worker) |
| Dead-letter replay | `unit/services/test_dlq_service.py`, `e2e/test_celery_rabbitmq.py` (opt-in) |

## Not done

- **Nothing here is run by CI yet.** `e2e/test_full_flow.py` (opt-in, `CIQ_E2E_FLOW=1`) drives a
  running Compose stack with real providers: four synthetic contracts for two vendors uploaded, read
  and indexed, a grounded answer with citations over the live stream, a follow-up, a question the
  documents cannot answer, history, a contract analysis, two vendor assessments, a comparison, and every
  UI page rendered with that data. It spends real tokens (cents with a small model) and was run
  against the stack with OpenAI `gpt-4o-mini` and `text-embedding-3-small`.
- **Ten consecutive CI runs without a flaky failure**, and **a suite under ten minutes in CI**: the
  integration suite is not in CI yet (M18), and locally the whole suite takes about 30 minutes.
- **Tuning the thresholds from the evaluation**: the offline numbers come from fake providers, so
  they say nothing about where real scores fall.
