# CovenantIQ — System Design

A detailed walkthrough of the system as built: what each piece is, how data moves through it, and
*why* that piece exists rather than an obvious alternative. For a one-page summary see `logic.md`;
for what V1 does feature-by-feature see `ProjectSpecV1.md`; for the decision log see `docs/adr/`.

---

## 1. What the system does

CovenantIQ reviews procurement contracts for a signed-in user:

1. **Ingests** a contract (PDF/Word) into structured, searchable text.
2. **Answers questions** about it in chat, with citations checked against the source text, and
   declines when the documents do not support an answer.
3. **Analyses** a contract against a rulebook of clause risks (liability caps, termination,
   data protection, ...), producing findings with evidence.
4. **Scores a vendor's** risk deterministically from its contracts' findings, across 6 dimensions.
5. **Compares vendors** side by side, only where the evidence supports it. *(Removed from V1 —
   see §11.)*

Everything is scoped to the signed-in user; nothing is shared across accounts.

---

## 2. Design goals, and the tension between them

| Goal | Tension it creates |
|---|---|
| Answers must be trustworthy, not fluent-sounding guesses | Generation has to be slower and willing to refuse |
| Risk scores must be explainable and reproducible | The model cannot be allowed to compute them |
| The system must survive a worker dying mid-job | Every write needs to be resumable or idempotent |
| One codebase, easy to reason about | Must not become a tangle of cross-imports as it grows |
| Real contracts are long, cross-referenced legal text | Naive fixed-size chunking destroys meaning |

Every architectural choice below exists to resolve one of these tensions.

---

## 3. High-level architecture

```
                                   ┌─────────────────────────┐
                                   │   Streamlit UI (web)     │
                                   │   frontend/              │
                                   └───────────┬──────────────┘
                                               │ HTTP + SSE only
                                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                         FastAPI application (api)                        │
│  routers → deps (auth, rate limit) → services → repositories → Postgres  │
│                                   │                                       │
│                     chat reply: an asyncio.Task running the              │
│                     LangGraph chat graph, streamed over a                │
│                     Redis Stream to the browser via SSE                  │
└───────────┬───────────────────────────────┬──────────────────────────────┘
            │ publish                       │ reach data only through services
            ▼                               ▼
   ┌─────────────────┐            ┌────────────────────────────┐
   │    RabbitMQ      │           │   PostgreSQL 16 + pgvector  │
   │ 4 work queues +  │           │   (+ pg_trgm, citext)       │
   │ 4 dead-letter Qs │           │   source of truth           │
   └────────┬─────────┘           └────────────┬───────────────┘
            │ deliver                          │ read/write
            ▼                                  │
   ┌─────────────────────┐                     │
   │  worker-ingest       │◄────────────────────┘
   │  (Docling extraction,│
   │   chunk, embed)      │
   ├─────────────────────┤
   │  worker-analysis     │  (contract analysis, vendor assessment;
   │  (LangGraph, LLM)     │   checkpoints to Postgres)
   └──────────┬──────────┘
              │ optional, over HTTP
              ▼
   ┌─────────────────────┐        ┌───────────────────────┐
   │  reranker container  │        │        Redis           │
   │ (cross-encoder model,│        │ cache · chat stream ·  │
   │  its own CPU budget) │        │ locks · rate limits    │
   └─────────────────────┘        └───────────────────────┘
```

**Why a modular monolith, not microservices** (ADR-001): the whole domain (ingest → retrieve →
analyse → score) shares one data model and one set of invariants (ownership, idempotency). Splitting
it into services would mean distributed transactions for no benefit at this scale. Instead the
*code* is modular (strict import layering, enforced by `import-linter`, 15 contracts) and the
*runtime* is modular (separate containers for the API, each worker pool, and the reranker), so each
can be scaled, restarted, or resourced independently without the cost of network calls between
domain steps.

---

## 4. The package layers, and why each exists

```
core        settings, enums, error types, logging, security, Redis key names   (imports nothing)
   ▲
db          SQLAlchemy models, repositories, unit of work                       (imports core)
   ▲
ingestion   upload validation, extraction, chunking, injection scanning
ai          LLM / embedding / reranker adapters behind small interfaces
rag         hybrid retrieval, evidence gate, citation validation
analysis    pure, deterministic contract-risk and vendor-scoring engines
   ▲
graphs      LangGraph workflows that call ai/rag/analysis through ports
memory      Redis session window + PostgreSQL summary
streaming   SSE events, Redis Stream publisher/consumer, the reply lock
   ▲
services    the use cases: upload, chat submit, start an analysis, ...
   ▲
workers     Celery app, queue topology, task entry points
api         FastAPI app: routers, schemas, the composition root (container.py)
```

| Layer | Why it is separate |
|---|---|
| `core` | Everything else needs it; it needs nothing. A settings typo is a startup failure, not a runtime surprise. |
| `db` | The **only** place SQL is written. Repositories take an `OwnershipScope`, so "forgot the tenant filter" is a missing parameter, not a missing `WHERE` clause a reviewer has to catch. |
| `ingestion` | Pure, mostly offline logic (parsing, chunking) kept out of the orchestration layer so it is testable without a database or a model. |
| `ai` | One narrow interface per capability (`LLMProvider`, `EmbeddingProvider`, `Reranker`). Everything above this line is written against the interface, so a vendor swap (Anthropic → OpenAI) is a config change, not a rewrite, and tests run against fakes with no network. |
| `rag` | Retrieval and grounding is a complete sub-problem (fuse, rank, gate, cite) that both chat and analysis consume, and that must be testable without ever calling a model. |
| `analysis` | **Pure functions.** No I/O, no model call, no clock. This is where "the model is not allowed to invent a risk score" is enforced structurally, not by convention. |
| `graphs` | Orchestration only. A node reads state, calls one engine/port, returns an update — nothing decides anything here, so the control flow (what runs after what, what retries) is easy to read on its own. |
| `memory` / `streaming` | Infrastructure for *this* product's two cross-cutting needs (remembering a conversation, streaming a reply) kept out of `services` so they can be tested and reasoned about alone. |
| `services` | Where transactions and ownership checks live. Both HTTP routes and Celery tasks call the same service methods, so behaviour is identical whichever triggered it. |
| `workers` / `api` | The only two places that know about a specific runtime (Celery, FastAPI). Swappable in principle without touching the domain. |

An import-linter contract enforces the upward-only rule above (e.g. "graphs may not import db",
"api reaches data only through services"); a build fails if someone takes a shortcut.

---

## 5. Data model

```
users ──< vendors ──< documents ──< document_clauses ──< document_chunks (vector + tsvector)
  │                        │
  │                        ├──< document_processing_jobs
  │                        │
  │                        └──< analysis_jobs ──< contract_findings ──< citations
  │                                   │
  │                                   └──< vendor_risk_assessments ──< vendor_risk_dimensions ──< citations
  │
  └──< conversations ──< conversation_documents (scope)
             │
             └──< messages ──< citations
```

- **Parent/child text storage**: `document_clauses` are the readable, citable unit (a contract
  section); `document_chunks` are small pieces of a clause, each with its own embedding and
  tsvector. Retrieval searches chunks (precision) but the model reads whole parent clauses
  (context) — see §7.
- **One `citations` table**, reused by chat messages, contract findings and vendor-assessment
  dimensions (`CHECK (num_nonnulls(message_id, finding_id, risk_dimension_id) = 1)`), so "where did
  this claim come from" is answered the same way everywhere.
- **PostgreSQL enums** for every closed vocabulary (`job_status`, `clause_status`, `severity`, ...),
  so an invalid value is a database error, not a typo that silently matches nothing.
- Primary keys are **UUIDv7** (time-ordered), so they sort by creation and keep indexes compact,
  while still being unguessable and safe to expose in URLs.

---

## 6. Why PostgreSQL does (almost) everything (ADR-002)

A natural instinct is "vectors need a vector database." Instead:

- **pgvector** gives an HNSW index for cosine similarity, inside the same transaction as everything
  else.
- **PostgreSQL full-text search** (`tsvector`, `english` config, `websearch_to_tsquery`) gives exact
  lexical matching — legal text needs this ("Schedule 3", "indemnify", a defined term) alongside
  semantic search.
- **pg_trgm / citext** give fuzzy and case-insensitive matching where needed (e.g. email lookup).

**Why this beats a separate vector store**: no second system to keep consistent with the first, no
dual-write failure mode, one set of backups, one connection pool, and ownership filtering is a
plain SQL `WHERE` clause instead of a second access-control system. The cost is a ceiling on scale
that a dedicated vector database would push further out — an explicit, accepted trade-off for V1.

**Redis is deliberately *not* a database.** It holds:

| What | Why Redis, and why losing it is safe |
|---|---|
| Chat session window + rolling summary | Speed only; rebuilt from PostgreSQL deterministically if missing |
| Query-embedding and rerank-score caches | Pure optimisation; a miss just costs one extra model call |
| The chat reply stream (SSE buffer) | PostgreSQL holds the final message; a listener that misses events replays from there |
| The per-conversation reply lock | If the holder dies, the lock expires on its own (TTL) |
| Fixed-window rate limit counters | Fails open if Redis is down, rather than locking everyone out |

Every one of these rows is: **if Redis disappears, the system gets slower or stricter, never wrong.**
That single rule is why Redis never needs its own backup strategy.

---

## 7. The chat / RAG pipeline (the heart of the system)

```
question
   │
   ▼
load conversation memory (Redis window + summary; rebuilt from PostgreSQL if stale)
   │
   ▼
classify intent ──► greeting/direct answer, or "no documents" ──► abstain
   │
   ▼  (document question)
rewrite into a standalone query (resolves "and the termination clause?" using history)
   │
   ▼
┌─────────────────────────── retrieval ───────────────────────────┐
│  dense search (pgvector, top 30)   lexical search (FTS, top 30)  │  ← run concurrently
│                    │                        │                    │
│                    └──────────┬─────────────┘                    │
│                 Reciprocal Rank Fusion (k = 60, clause-type boost)│
│                               │                                   │
│                    optional rerank (cross-encoder, 3s budget)     │
│                               │                                   │
│                     top 8 → expand to parent clauses              │
└────────────────────────────────┬──────────────────────────────────┘
                                  ▼
                    evidence gate: strong / moderate / weak / none
                                  │
                  weak or none ──┴── abstain (template, NO model call)
                                  │
                        strong/moderate
                                  ▼
          build numbered <source id="C1" ...> blocks (sources marked untrusted,
          a per-request canary string embedded to detect prompt injection)
                                  │
                                  ▼
                    model streams an answer with [C#] markers
                                  │
                                  ▼
       deterministic citation validator (no model call):
       every marker real? every cited sentence supported? every number present in its source?
                                  │
               ┌──────────────────┼───────────────────────┐
          fully supported   partially supported      unsupported / canary leaked
               │                  │                       │
          shown as-is      shown with a notice        replaced by abstention
```

**Why hybrid retrieval, not just embeddings**: embeddings miss exact terms (a clause number, a
defined term) that a lawyer's question names directly; full-text search misses paraphrase.
Reciprocal Rank Fusion combines the two by *rank*, not raw score, because cosine similarity and a
tsvector rank are on incomparable scales — averaging the scores would be meaningless, averaging the
ranks is stable.

**Why an evidence gate before generation, not after**: calling the model and then discarding its
answer wastes money and still risks it anchoring on weak context. Gating first means a genuinely
unsupported question costs a template, not a model call.

**Why a separate, deterministic citation validator instead of asking the model to self-check**:
models are unreliable judges of their own output, and a second model call to "check" the first just
moves the hallucination risk one step, at double the cost. Splitting the text into sentences,
matching markers to sources, and checking word overlap and that cited numbers actually appear in
the source is cheap, deterministic, and testable with ordinary unit tests.

**Why parent-clause expansion**: chunks are small enough to embed precisely, but a model reading
only a 350-token fragment loses the surrounding clause's context (a cross-reference, a defined
term introduced two sentences earlier). Retrieval finds the chunk; generation reads its parent.

**Why streaming goes through Redis and not straight to the HTTP response**: the reply keeps being
generated even if the browser disconnects (the producer is a plain asyncio task in the API
process, decoupled from any one HTTP connection); a reconnect resumes exactly where it left off
because Redis Stream entry IDs double as SSE event IDs; and if Redis loses the stream entirely, a
*finished* reply is replayed from PostgreSQL, which is the actual source of truth. See §10 for why
this isn't RabbitMQ.

---

## 8. Contract analysis and vendor scoring — keeping the model out of the arithmetic

```
contract analysis graph                         vendor assessment graph
───────────────────────                         ───────────────────────
load document + 30-rule ruleset                  load vendor's ready documents
       │                                                │
plan rule batches (4 rules/call)                 ensure each has a current contract analysis
       │                                                │  (starts one if missing; waits, bounded)
Send → assess_rule_batch × N  (parallel)                │
       │ (model: category + quote,                map findings → 26 signals in 6 dimensions
       │  never a number)                                │
aggregate batch results                          Send → extract_dimension_signals × 6 (parallel)
       │                                                │  (model: status + quote, never a number)
extract obligations & deadlines                  validate_signal_evidence
       │                                                │
detect internal contradictions                   aggregate across the vendor's documents
  (candidates found by rule, confirmed           (conservative; order of documents cannot matter)
   or dismissed by one model call)                      │
       │                                         score_deterministic  ← PURE CODE, no model, no I/O
scan unusual provisions                                 │
       │                                         explain_dimensions (model writes prose only)
validate_evidence                                       │
       │                                         persist_assessment
build_findings  (absence requires proof of search;
  anything less honestly says "insufficient
  evidence", never "absent")
       │
persist_findings
```

Both graphs are **checkpointed to PostgreSQL** (one checkpoint thread per job). If a worker dies
after 4 of 7 rule batches, the redelivered task resumes the same thread — the four finished batches
are not re-run, and not re-billed.

**The one rule that explains every number in this part of the system:** *the model classifies and
quotes; code computes.* `analysis/contract/status.py` and `analysis/vendor/scoring.py` have no
network and no model call, and are property-tested: the order documents arrive in cannot change a
score, a favourable signal can never raise risk, and an absent signal is a gap (lowers completeness)
unless a rule explicitly names it as a risk in itself (e.g. "no liability cap"). This is why a risk
score can be explained to a customer in plain terms instead of "the AI said so."

**Why `insufficient_evidence` is a first-class status, not a fallback to `absent`:** calling
something absent is a claim — it implies a thorough search found nothing. The system only makes that
claim when extraction was complete, several searches found nothing close, and no pattern hints the
clause exists. Anything short of that is an honest "could not tell," which is the harder and more
defensible engineering choice but the one that doesn't create false confidence.

---

## 9. The event loop model (why Python's biggest async pitfall doesn't bite here)

```
uvicorn worker 1 (its own loop) ──┐
uvicorn worker 2 (its own loop) ──┤  each with its own Container: engine, Redis clients, LLM client
                                  │
Celery prefork child A ──────────┤  one long-lived loop per child (NullPool DB engine);
Celery prefork child B ──────────┤  tasks hand their coroutine to run_async(coro) on that loop
Celery prefork child C ──────────┘  (never "asyncio.run() per task" — would thrash connections)
```

The classic bug this avoids: creating a new event loop per request/task recreates its async
resources (DB connections, HTTP clients) every time, which is slow and leaks connections under
load. Here, exactly one loop and one container are built per process at startup; a Celery task is
a thin synchronous wrapper that submits its coroutine to that process's already-running loop.
Windows needs the selector event loop (not the default proactor) because `psycopg`, used by the
LangGraph checkpointer, requires it — handled once, in `workers/runtime.new_event_loop()`.

---

## 10. Reliability: RabbitMQ, Celery, and why chat skips both

```
API: commit job row (queued) ─┐
                               ├─ two separate steps — a crash between them
                               ▼   leaves a queued job with nothing published
publish task to RabbitMQ ─────┘        (closed by the 60s sweeper, see below)
        │
        ▼
worker delivery ── claim via compare-and-set (UPDATE ... WHERE status = 'queued')
        │              only one worker can win this race; no lock server needed
        ▼
heartbeat every 30s while running
        │
   worker dies ──► heartbeat goes stale (>10 min) ──► another worker's sweep reaps and republishes
        │                                              (capped at 5 attempts)
   task raises ──► acks_late + reject_on_worker_lost ──► redelivered, or
                    PermanentTaskError ──► dead-lettered to {queue}.dlq (inspect/replay via CLI)
```

**Why RabbitMQ for background jobs**: durable queues, per-message acknowledgement, and automatic
dead-lettering per queue — exactly what "a job must not be silently lost" needs, with no bespoke
retry bookkeeping.

**Why chat does *not* use it**: a reply must start generating within milliseconds of the request,
and a broker hop adds latency for no benefit here — there's no multi-stage pipeline to coordinate,
just "start writing, stream what you write." Chat's safety net is instead the per-conversation
Redis lock (so two replies can never interleave) plus PostgreSQL as the record of what was actually
said, which is enough for a single logical step that fails all-or-nothing.

**Why correctness lives in PostgreSQL, not in the broker**: a message is only ever a *nudge*. The
job row's state machine (compare-and-set transitions) is what actually decides whether work
proceeds, so a duplicate delivery, a redelivery after a crash, or a replayed dead letter all do the
right thing — whichever message arrives first wins the claim, and the rest become harmless no-ops.

---

## 11. The LLM/embedding/reranker layer — ports and adapters (ADR-011, ADR-012)

```
            ┌────────────────────┐
graphs ───► │   LLMProvider port  │ ◄─── AnthropicProvider │ OpenAIProvider │ FakeLLMProvider
  rag  ───► │ EmbeddingProvider   │ ◄─── Voyage │ OpenAI-compatible │ Fake
            │   Reranker port     │ ◄─── Cohere │ local cross-encoder (own container) │ none │ Fake
            └────────────────────┘
```

Nothing above this line knows which vendor answers a request. Consequences:

- **Switching the whole stack from Anthropic to OpenAI was an environment-variable change**, not a
  code change — proven in this project when the Docker stack was run end-to-end on OpenAI
  `gpt-4o-mini` and `text-embedding-3-small`.
- **Per-route model choice**: `config/llm_routes.yaml` assigns a model and "thinking effort" per
  kind of call (`classify`, `rewrite`, `answer`, `rule_assessment`, ...), so a cheap classification
  step and an expensive grounded answer can use different models without touching code.
- **Every test runs against a fake**, so the test suite costs nothing and needs no network, while
  still exercising the exact same code path production uses.
- **The local reranker runs in its own container** (`covenantiq rerank-serve`), never inside the API
  or a worker — reranking is CPU-heavy (PyTorch, ~14 pairs/sec on a desktop CPU) and would otherwise
  stall the event loop and compete with Docling extraction for CPU. A unit test fails if the main
  application process ever imports `torch`.
- **Reranking is optional everywhere it's used.** Off, slow, or failing, retrieval keeps the fused
  order and the evidence gate falls back to a stricter rule ("the top result must appear in both
  individual rank lists"), so a reranker outage degrades strictness, not availability.

---

## 12. Security and multi-tenancy

- **Ownership is structural, not a habit.** Every repository method that touches user data takes an
  `OwnershipScope`; a dedicated test (`test_scope_contract.py`) fails if a method lacks one.
- **A foreign id is always 404, never 403.** Confirming "that vendor exists, you just can't see it"
  would leak information; a 17-case ownership matrix test hits every route with another user's ids
  and asserts 404 everywhere, and fails the build if a *new* route isn't classified into the matrix
  at all — so the protection can't be silently skipped by a future endpoint.
- **Passwords**: Argon2 with rehash-on-login, a 12–128 character policy, and a common-password
  deny-list.
- **Tokens**: JWT access tokens only for V1 (ADR-015) — no refresh tokens, no sessions server-side
  to manage; simplest option that meets the requirement.
- **Prompt injection**: ingested text is scored for "reads like an instruction to an AI" and
  invisible characters are stripped; flagged text is still stored (it may be legitimate contract
  language) but wrapped with a stronger warning in the prompt. A canary string unique to each
  request is embedded in the system prompt — if it ever appears in the model's answer, that answer
  is replaced by a refusal, because the model has leaked its instructions rather than answered the
  question.
- **Logging**: secrets are redacted, and tracebacks never print local variables (`show_locals=False`)
  — contract text and prompts live in local variables, and a crash must not leak them into logs.

---

## 13. Testing strategy — why each layer gets a different kind of test

| Layer | Kind of test | Why this kind fits |
|---|---|---|
| `analysis` (scoring, status) | Property-based (Hypothesis) | Pure functions with invariants ("order never matters", "absence never helps a score") are exactly what property tests are for |
| `rag` (fusion, citations) | Unit, no network | Fully deterministic; a fake retriever is enough |
| `graphs` | Unit on fakes + scripted LLM responses | Tests the control flow (resume, degrade-one-rule-not-the-batch) without spending a token |
| `db`, migrations | Integration, real PostgreSQL/pgvector | Enum types, CHECK constraints and HNSW behaviour can't be faked honestly |
| API | Integration, real stack, fake model | Exercises ownership, status codes and the whole request path |
| Whole product | `e2e/test_full_flow.py`, opt-in, real provider | The one test that spends real tokens and proves the system works beyond its own fakes |

The **ownership matrix** and the **offline evaluation gate** (a committed baseline over a labelled
dataset, run with fakes) are the two tests specifically designed to catch regressions that unit
tests, by construction, cannot: a newly-added route that forgot ownership, and a prompt or
threshold change that quietly made answers worse.

---

## 14. Current status

Milestones M1–M17 are complete and committed; M18 (CI/CD) and M19 (production deployment) have not
been started. See `progress.md` for the detailed, milestone-by-milestone log of what was built,
found, and fixed, and `ProjectSpecV1.md` for the honest list of what is still unverified (evidence
thresholds are uncalibrated defaults, the reranker has not been load-tested, cost has not been
measured).

A vendor **comparison** feature (comparing two or more assessed vendors) was built in M13 and
verified end-to-end in Docker, but is **not part of V1** as this document describes it: it was
removed at the user's request after verification (code, schema, UI page and tests deleted; a
migration drops the `vendor_comparisons` table and the `vendor_comparison` enum value — §8 above
already describes the system without it). Vendor **assessment** (single-vendor scoring) is
unaffected and remains in the product.

---

## 15. Where to read more

`logic.md` (one-page map) → `src/covenantiq/*/logic.md` (per-package intuition) → `docs/adr/`
(27 numbered decisions, the *why* behind each one) → `docs/runbook.md` (operating it) →
`docs/test-matrix.md` (risk → test) → `INTERVIEW_PREP.md` (teaching-depth walkthrough) →
`PROJECT_SPEC.md` (the original 5000-line plan) → `ProjectSpecV1.md` (what V1 actually is).
