"""Serving a reply's events to a listener (spec 26.4, 26.5).

    1. the caller has already checked the message is the listener's (see `StreamService`)
    2. the reply is final in PostgreSQL and its stream has expired: replay it from PostgreSQL
    3. otherwise read the Redis Stream from `Last-Event-ID` (or the start), blocking for news
    4. stop at the terminal event; if nothing arrives for `idle_timeout`, look at PostgreSQL: a
       final reply is finished from there, anything else is a stalled producer (`STREAM_STALLED`)

A listener that goes away only ends its own loop: the producer neither knows nor cares. Reconnecting
with the last id gives neither duplicates nor gaps, because an id is the stream entry id.
"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from covenantiq.core import redis_keys
from covenantiq.core.enums import MessageStatus
from covenantiq.streaming.events import TERMINAL

BLOCK_MS = 15_000
READ_COUNT = 100
_FINAL = (MessageStatus.COMPLETED, MessageStatus.FAILED, MessageStatus.INTERRUPTED)


@dataclass(frozen=True)
class Frame:
    """One server-sent event: `id`, `event` and the JSON `data`."""

    id: str
    event: str
    data: str


@dataclass(frozen=True)
class StoredReply:
    """What PostgreSQL knows of an assistant message: enough to replay it."""

    message_id: UUID
    conversation_id: UUID
    seq: int
    status: MessageStatus
    grounding_status: str | None
    content: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def final(self) -> bool:
        return self.status in _FINAL


Loader = Callable[[], Awaitable[StoredReply]]
Disconnected = Callable[[], Awaitable[bool]]


def _data(**fields: Any) -> str:
    return json.dumps(fields, default=str)


def replay_frames(reply: StoredReply, *, terminal_only: bool = False) -> list[Frame]:
    """The events of a finished reply as they would have been streamed, with ids `0-1`, `0-2`...

    `message_start`, the whole text as one `token`, the citations, then the terminal event: a
    `message_complete` for a completed reply, an `error` for one that failed or was interrupted. A
    client treats `message_start` as "forget what you have" and `message_complete.content` as the
    truth, so a replay is indistinguishable from a stream that was missing its tokens.
    """
    mid = str(reply.message_id)
    items: list[tuple[str, str]] = []
    if not terminal_only:
        items.append(
            (
                "message_start",
                _data(
                    event="message_start",
                    message_id=mid,
                    conversation_id=str(reply.conversation_id),
                    seq=reply.seq,
                ),
            )
        )
        if reply.content:
            items.append(
                ("token", _data(event="token", message_id=mid, text=reply.content, index=0))
            )
    if reply.status is MessageStatus.COMPLETED:
        items.extend(
            ("citation", _data(event="citation", message_id=mid, citation=c))
            for c in reply.citations
        )
        items.append(
            (
                "message_complete",
                _data(
                    event="message_complete",
                    message_id=mid,
                    status=reply.status.value,
                    grounding_status=reply.grounding_status,
                    content=reply.content,
                    usage=reply.usage,
                    timings_ms={},
                ),
            )
        )
    else:
        interrupted = reply.status is MessageStatus.INTERRUPTED
        items.append(
            (
                "error",
                _data(
                    event="error",
                    message_id=mid,
                    code="INTERRUPTED" if interrupted else "GENERATION_FAILED",
                    message=(
                        "The server restarted before the answer was finished."
                        if interrupted
                        else "Something went wrong while preparing the answer."
                    ),
                    retryable=True,
                ),
            )
        )
    return [Frame(f"0-{i}", event, data) for i, (event, data) in enumerate(items, start=1)]


def _id_parts(entry_id: str) -> tuple[int, int] | None:
    try:
        left, right = entry_id.split("-", 1)
        return int(left), int(right)
    except ValueError:
        return None


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class StreamConsumer:
    def __init__(
        self,
        redis: Redis,
        *,
        env: str,
        idle_timeout: float = 90.0,
        block_ms: int = BLOCK_MS,
    ) -> None:
        # `redis` must be a client whose socket timeout is longer than `block_ms`
        self._redis = redis
        self._env = env
        self._idle = idle_timeout
        self._block_ms = block_ms

    async def frames(
        self,
        message_id: UUID,
        *,
        load: Loader,
        last_event_id: str | None = None,
        disconnected: Disconnected | None = None,
    ) -> AsyncIterator[Frame]:
        key = redis_keys.stream(self._env, message_id)
        reply = await load()
        if reply.final and not await self._redis.exists(key):
            after = _id_parts(last_event_id) if last_event_id else None
            resume = after if after is not None and after[0] == 0 else (0, 0)
            for number, frame in enumerate(replay_frames(reply), start=1):
                if number > resume[1]:
                    yield frame
            return

        position = last_event_id if last_event_id and _id_parts(last_event_id) else "0-0"
        if reply.final and await self._already_ended(key, position):
            return  # a reconnect after the terminal event: there is nothing more to say
        last_seen = monotonic()
        while True:
            if disconnected is not None and await disconnected():
                return
            wait_ms = max(
                1, min(self._block_ms, int((self._idle - (monotonic() - last_seen)) * 1000))
            )
            answer: Any = await self._redis.xread({key: position}, count=READ_COUNT, block=wait_ms)
            entries: list[Any] = answer[0][1] if answer else []
            for entry_id, raw in entries:
                fields = {_text(k): _text(v) for k, v in raw.items()}
                position = _text(entry_id)
                event, data = fields["event"], fields["data"]
                yield Frame(position, event, data)
                if event in TERMINAL:
                    return
            if entries:
                last_seen = monotonic()
                continue
            if monotonic() - last_seen >= self._idle:
                reply = await load()
                if reply.final:
                    for frame in replay_frames(reply, terminal_only=True):
                        # keep the last real id, so a reconnect does not start over
                        yield Frame(position, frame.event, frame.data)
                else:
                    yield Frame(
                        position if position != "0-0" else "0-1",
                        "error",
                        _data(
                            event="error",
                            message_id=str(message_id),
                            code="STREAM_STALLED",
                            message="The answer stopped arriving. You can ask again.",
                            retryable=True,
                        ),
                    )
                return

    async def _already_ended(self, key: str, position: str) -> bool:
        """The stream's last entry is a terminal event and the listener has already seen it."""
        last: Any = await self._redis.xrevrange(key, count=1)
        if not last:
            return False
        entry_id, raw = last[0]
        fields = {_text(k): _text(v) for k, v in raw.items()}
        seen, latest = _id_parts(position), _id_parts(_text(entry_id))
        return (
            fields.get("event") in TERMINAL
            and seen is not None
            and latest is not None
            and (seen >= latest)
        )
