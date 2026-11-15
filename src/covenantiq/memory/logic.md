# memory: conversation memory (Redis window, PostgreSQL truth)

## What it is
What the chat graph remembers about earlier turns: a recent **window** of exchanges plus a rolling **summary** of older ones, kept fast in Redis and always rebuildable from PostgreSQL.

## Intuition
A model has no memory: every request must include the history. History grows without bound, but the prompt budget does not. So keep the most recent turns verbatim, fold older ones into a summary, and cache the result in Redis so each request is fast. **Redis is a cache; losing it costs latency, never correctness.**

## Files
| File | What it does |
|---|---|
| `base.py` | `MemoryContext` (window + summary + `extensions`) and the provider interface. `extensions` lets later versions add semantic or entity memory without touching the graph. |
| `manager.py` | The single entry point. `load(conversation)` returns the context (rebuilt from PostgreSQL if Redis is stale). `record_turn(...)` appends a finished exchange; if the window overflowed it starts a **background** summarisation. Summarising never blocks a reply. |
| `session_redis.py` | The Redis side. Two keys per conversation (both include the user id and refresh a TTL on every touch): `window` (a LIST of JSON turns) and `meta` (a HASH: `last_seq`, `summary`, `summary_through_seq`, `window_tokens`, `schema_v`). Any inconsistency (miss, version change, `last_seq` disagrees with PostgreSQL, corrupt entry) means rebuild. |
| `reconstruction.py` | Rebuilds a window from PostgreSQL only: take the stored summary and `summary_through_seq`, read completed messages after it newest-first until the token budget is full, and return older unsummarised ones as `overflow` for summarising. Deterministic and always possible. |
| `summarizer.py` | Folds the previous summary plus overflowed turns into a new summary via the `summarize` LLM route. |
| `factory.py` | Wiring. |

## Key ideas
- **Only finished exchanges count as memory.** A reply still streaming, failed or interrupted is excluded.
- **Order of writes for summaries**: summarise, write to PostgreSQL and commit, *then* mirror to Redis. If summarising fails, the Redis session is dropped, so the next load rebuilds and finds the unsummarised turns again. Nothing is lost.
- **Ownership first**: the conversation is verified in PostgreSQL before Redis is touched, and keys embed the user id.
- **Tested**: flushing Redis mid-conversation, the follow-up still sees the earlier turn.

## Interview soundbite
"Memory is a write-through cache pattern with a durable source of truth. Redis holds a token-bounded window and summary for speed; if it is flushed, I reconstruct deterministically from PostgreSQL. Summarisation is asynchronous so it never adds latency to a reply."
