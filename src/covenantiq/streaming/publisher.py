"""Where a chat reply's events go (spec 26.3).

The producer publishes through this interface and does not know who is listening. Two
implementations carry events: `InMemoryPublisher` (tests, a single-process run) and
`RedisStreamPublisher`, which appends them to a Redis Stream that the SSE consumer reads.

PostgreSQL, not the stream, is the record of a reply. A Redis failure while publishing is logged and
dropped: the producer goes on and the message still completes in PostgreSQL, and a listener that
missed events gets the reply from there.
"""

import asyncio
import contextlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from covenantiq.core import redis_keys
from covenantiq.core.logging import get_logger
from covenantiq.streaming.events import SSEEvent, Token, is_terminal

log = get_logger(__name__)


class StreamPublisher(Protocol):
    async def publish(self, message_id: UUID, event: SSEEvent) -> None: ...


class NullPublisher:
    async def publish(self, message_id: UUID, event: SSEEvent) -> None:
        return None


class InMemoryPublisher:
    """Keeps every event, per message, in order."""

    def __init__(self) -> None:
        self.events: dict[UUID, list[SSEEvent]] = defaultdict(list)

    async def publish(self, message_id: UUID, event: SSEEvent) -> None:
        self.events[message_id].append(event)

    def of(self, message_id: UUID) -> list[SSEEvent]:
        return list(self.events[message_id])

    def kinds(self, message_id: UUID) -> list[str]:
        return [e.event for e in self.events[message_id]]


@dataclass
class _Pending:
    """Token text waiting to be written, and the machinery that keeps it in order."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    text: str = ""
    first: Token | None = None
    timer: asyncio.Task[None] | None = None
    started: bool = False  # the stream key has been given its (long) safety expiry


class RedisStreamPublisher:
    """`XADD ciq:{env}:stream:{message_id} MAXLEN ~ 5000 * event <type> data <json>`.

    Token events are coalesced: what has built up is written when it reaches `flush_chars` or has
    waited `flush_interval` seconds, whichever comes first, and always before any other event, so
    the order of events never changes. A merged token carries the index of its first token. After
    the terminal event the key expires in `ttl_seconds`.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        env: str,
        ttl_seconds: int = 600,
        maxlen: int = 5000,
        flush_interval: float = 0.030,
        flush_chars: int = 64,
    ) -> None:
        self._redis = redis
        self._env = env
        self._ttl = ttl_seconds
        self._maxlen = maxlen
        self._interval = flush_interval
        self._chars = flush_chars
        self._pending: dict[UUID, _Pending] = {}

    def key(self, message_id: UUID) -> str:
        return redis_keys.stream(self._env, message_id)

    async def publish(self, message_id: UUID, event: SSEEvent) -> None:
        pending = self._pending.setdefault(message_id, _Pending())
        async with pending.lock:
            if isinstance(event, Token):
                if pending.first is None:
                    pending.first = event
                pending.text += event.text
                if len(pending.text) >= self._chars:
                    await self._flush(message_id, pending)
                elif pending.timer is None:
                    pending.timer = asyncio.create_task(self._flush_later(message_id, pending))
                return
            await self._flush(message_id, pending)
            await self._write(message_id, pending, event)
            if is_terminal(event):
                self._pending.pop(message_id, None)
                with contextlib.suppress(RedisError, OSError):
                    await self._redis.expire(self.key(message_id), self._ttl)

    async def _flush_later(self, message_id: UUID, pending: _Pending) -> None:
        await asyncio.sleep(self._interval)
        async with pending.lock:
            pending.timer = None
            await self._flush(message_id, pending, from_timer=True)

    async def _flush(
        self, message_id: UUID, pending: _Pending, *, from_timer: bool = False
    ) -> None:
        if pending.timer is not None and not from_timer:
            pending.timer.cancel()
            pending.timer = None
        if pending.first is None:
            return
        merged = Token(message_id=message_id, text=pending.text, index=pending.first.index)
        pending.first, pending.text = None, ""
        await self._write(message_id, pending, merged)

    async def _write(self, message_id: UUID, pending: _Pending, event: SSEEvent) -> None:
        key = self.key(message_id)
        try:
            await self._redis.xadd(
                key,
                {"event": event.event, "data": json.dumps(event.model_dump(mode="json"))},
                maxlen=self._maxlen,
                approximate=True,
            )
            if not pending.started:
                pending.started = True
                # a producer that dies leaves no terminal event: the key must not live forever
                await self._redis.expire(key, self._ttl * 6)
        except (RedisError, OSError):
            log.warning("stream.publish_failed", kind=event.event, exc_info=True)
