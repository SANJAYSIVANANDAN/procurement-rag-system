# streaming: live replies over SSE using Redis Streams

## What it is
Moves a chat reply from the code that generates it to the browser that watches it: typed events, a Redis Stream as the buffer, a consumer that serves SSE, and a lock that allows one reply at a time per conversation.

## Intuition
The **producer** (the chat graph, running as an asyncio task in the API) and the **listener** (an HTTP SSE request) are decoupled. The producer writes events to a Redis Stream and does not know who is listening. The listener reads from the stream. So the reply keeps generating if the browser disconnects, a reconnect can pick up exactly where it left off, and PostgreSQL remains the final record.

## Event order
```
message_start -> (status | token)* -> citation* -> message_complete | error
```
Exactly one terminal event ends a stream.

## Files
| File | What it does |
|---|---|
| `events.py` | Pydantic models for every SSE event and the ordering rules. The runner validates each event before publishing. |
| `publisher.py` | `Publisher` interface with `InMemoryPublisher` (tests) and `RedisStreamPublisher`. The Redis one **coalesces tokens** (flush every 30 ms or 64 chars) to avoid one Redis write per token, `XADD`s with `MAXLEN ~ 5000`, and expires the stream 600 s after the terminal event (with a longer safety expiry on the first write). A Redis failure is logged and dropped: the message still completes in PostgreSQL. |
| `consumer.py` | `StreamConsumer.frames`: serves events to a listener. Uses the **Redis Stream entry id as the SSE `id`**, so `Last-Event-ID` resumes with no gaps or duplicates. Blocks on `XREAD BLOCK`. If the stream has expired but the message is final, **replays from PostgreSQL** (ids `0-N`). If nothing arrives within the idle timeout and PostgreSQL says the reply is not final, emits `STREAM_STALLED`. A reconnect after the terminal event returns nothing. |
| `locks.py` | One reply at a time per conversation: `SET key token NX EX ttl` to acquire; Lua scripts to release or extend **only if the token matches**, so a late release can never free someone else's lock. A heartbeat every 30 s extends it during long replies; if the holder dies it expires after 120 s. |

## Key ideas
- **Why SSE, not WebSocket**: replies are one-way; SSE is plain HTTP, works through proxies and has built-in reconnect with `Last-Event-ID`.
- **Why a Redis Stream, not pub/sub**: pub/sub drops messages for absent listeners; a stream keeps them, so resume works.
- **Why not RabbitMQ for chat**: it needs low latency and the reply must start immediately; a queue hop adds delay, while the lock and PostgreSQL give the needed safety.
- **Ownership** is checked in `services/stream_service.py` (PostgreSQL) before this package is reached.
- **409 vs 503**: lock held means a reply is in progress (409); Redis down means nothing is persisted and the request fails (503).

## Interview soundbite
"The stream is a buffer, not the source of truth. Entry ids double as SSE ids, so reconnect-and-resume is exact. If Redis loses the stream, the consumer replays the finished reply from PostgreSQL."
