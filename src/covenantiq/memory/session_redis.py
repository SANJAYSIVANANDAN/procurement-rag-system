"""The Redis session window (spec 23.3 to 23.8).

Two keys per conversation, both carrying the user id and both with a TTL that every read and write
refreshes:

    window  LIST   JSON turns, oldest first
    meta    HASH   last_seq, summary, summary_through_seq, window_tokens, schema_v

Redis is a cache. Anything wrong with it (a miss, a schema change, a `last_seq` that disagrees with
PostgreSQL, a summary that is out of step, a corrupt entry, token counts that do not add up) means
the window is rebuilt from PostgreSQL and rewritten, with the reason logged. The ownership check
happens in PostgreSQL before this class is reached, and no request path scans keys.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from redis.asyncio import Redis

from covenantiq.core import redis_keys
from covenantiq.core.logging import get_logger
from covenantiq.db.scope import OwnershipScope
from covenantiq.memory.base import (
    SCHEMA_VERSION,
    CompletedTurn,
    ConversationState,
    MemoryContext,
    SessionTurn,
)
from covenantiq.memory.reconstruction import Reconstruction, Reconstructor

log = get_logger(__name__)


@dataclass
class SessionLoad:
    context: MemoryContext
    rebuilt_because: str | None = None  # None when Redis was consistent
    overflow: list[SessionTurn] = field(default_factory=list)  # to summarise (rebuild only)


@dataclass
class RecordResult:
    overflow: list[SessionTurn] = field(default_factory=list)  # turns pushed out, oldest first
    rebuilt_because: str | None = None


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class SessionMemoryProvider:
    name = "session"

    def __init__(
        self,
        redis: Redis,
        reconstructor: Reconstructor,
        *,
        env: str,
        ttl_seconds: int,
        window_tokens: int,
    ) -> None:
        self._redis = redis
        self._reconstructor = reconstructor
        self._env = env
        self._ttl = ttl_seconds
        self._budget = window_tokens

    def _keys(self, scope: OwnershipScope, conversation_id: UUID) -> tuple[str, str]:
        return (
            redis_keys.session_window(self._env, scope.owner_id, conversation_id),
            redis_keys.session_meta(self._env, scope.owner_id, conversation_id),
        )

    # ---- reading ----------------------------------------------------------------------------

    async def load_session(self, scope: OwnershipScope, state: ConversationState) -> SessionLoad:
        window_key, meta_key = self._keys(scope, state.conversation_id)
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.hgetall(meta_key)
            pipe.lrange(window_key, 0, -1)
            pipe.expire(meta_key, self._ttl)  # a read keeps an active conversation warm
            pipe.expire(window_key, self._ttl)
            meta, raw_turns, _, _ = await pipe.execute()

        turns, problem = self._check(state, meta, raw_turns)
        if problem is None and turns is not None:
            return SessionLoad(MemoryContext(summary=state.summary, recent_turns=turns))

        rebuilt = await self._rebuild(scope, state, problem or "unknown")
        return SessionLoad(
            MemoryContext(summary=state.summary, recent_turns=rebuilt.turns),
            rebuilt_because=problem,
            overflow=rebuilt.overflow,
        )

    @staticmethod
    def _check(
        state: ConversationState, meta: dict[Any, Any], raw_turns: list[Any]
    ) -> tuple[list[SessionTurn] | None, str | None]:
        """The turns, or the reason they cannot be trusted."""
        if not meta:
            return None, "miss"
        fields = {_text(k): _text(v) for k, v in meta.items()}
        try:
            if int(fields.get("schema_v", "0")) != SCHEMA_VERSION:
                return None, "schema_version"
            if int(fields["last_seq"]) != state.last_completed_seq:
                return None, "last_seq_mismatch"
            if int(fields.get("summary_through_seq", "0")) != state.summary_through_seq:
                return None, "summary_out_of_step"
            recorded_tokens = int(fields["window_tokens"])
        except (KeyError, ValueError):
            return None, "corrupt_meta"
        try:
            turns = [SessionTurn.model_validate_json(raw) for raw in raw_turns]
        except ValidationError:
            return None, "corrupt_turn"
        if [t.seq for t in turns] != sorted(t.seq for t in turns):
            return None, "out_of_order"
        if sum(t.tokens for t in turns) != recorded_tokens:
            return None, "token_count_mismatch"
        return turns, None

    # ---- writing ----------------------------------------------------------------------------

    async def record_turn(
        self, scope: OwnershipScope, conversation_id: UUID, turn: CompletedTurn
    ) -> RecordResult:
        """Append a finished exchange, then fold turns that no longer fit into `overflow`.

        Called after the exchange is committed in PostgreSQL. If the session is no longer in Redis
        (it expired mid-generation), appending would leave a window with only the last exchange,
        so it is rebuilt from PostgreSQL instead, which already contains it.
        """
        window_key, meta_key = self._keys(scope, conversation_id)
        if not await self._redis.exists(meta_key):
            state = await self._reconstructor.state(scope, conversation_id)
            if state is None:
                return RecordResult()
            rebuilt = await self._rebuild(scope, state, "expired_before_record")
            return RecordResult(rebuilt.overflow, "expired_before_record")

        added = turn.user.tokens + turn.assistant.tokens
        async with self._redis.pipeline(transaction=True) as pipe:  # MULTI: all or nothing
            pipe.rpush(window_key, turn.user.model_dump_json(), turn.assistant.model_dump_json())
            pipe.hincrby(meta_key, "window_tokens", added)
            pipe.hset(meta_key, "last_seq", turn.assistant.seq)
            pipe.expire(meta_key, self._ttl)
            pipe.expire(window_key, self._ttl)
            await pipe.execute()
        return RecordResult(await self._trim(window_key, meta_key))

    async def _trim(self, window_key: str, meta_key: str) -> list[SessionTurn]:
        """Pop the oldest turns while the window is over budget, always keeping the last two."""
        overflow: list[SessionTurn] = []
        while True:
            used = int(_text(await self._redis.hget(meta_key, "window_tokens") or 0))
            if used <= self._budget or await self._redis.llen(window_key) <= 2:
                return overflow
            raw = await self._redis.lpop(window_key)
            if raw is None or isinstance(raw, list):
                return overflow
            turn = SessionTurn.model_validate_json(raw)
            await self._redis.hincrby(meta_key, "window_tokens", -turn.tokens)
            overflow.append(turn)

    async def set_summary(
        self, scope: OwnershipScope, conversation_id: UUID, summary: str, through_seq: int
    ) -> None:
        """Mirror a summary that PostgreSQL already holds. Does nothing if the session is gone."""
        _, meta_key = self._keys(scope, conversation_id)
        if await self._redis.exists(meta_key):
            await self._redis.hset(
                meta_key, mapping={"summary": summary, "summary_through_seq": through_seq}
            )

    async def invalidate(self, scope: OwnershipScope, conversation_id: UUID) -> None:
        """Forget a session, so the next load rebuilds it from PostgreSQL."""
        await self._redis.delete(*self._keys(scope, conversation_id))

    # ---- rebuilding -------------------------------------------------------------------------

    async def _rebuild(
        self, scope: OwnershipScope, state: ConversationState, reason: str
    ) -> Reconstruction:
        rebuilt = await self._reconstructor.reconstruct(scope, state)
        window_key, meta_key = self._keys(scope, state.conversation_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(window_key, meta_key)
            if rebuilt.turns:
                pipe.rpush(window_key, *(t.model_dump_json() for t in rebuilt.turns))
            pipe.hset(
                meta_key,
                mapping={
                    "last_seq": state.last_completed_seq,
                    "summary": state.summary or "",
                    "summary_through_seq": state.summary_through_seq,
                    "window_tokens": sum(t.tokens for t in rebuilt.turns),
                    "schema_v": SCHEMA_VERSION,
                },
            )
            pipe.expire(meta_key, self._ttl)
            pipe.expire(window_key, self._ttl)
            await pipe.execute()
        log.info(
            "memory.rebuilt",
            reason=reason,
            conversation_id=str(state.conversation_id),
            turns=len(rebuilt.turns),
            overflow=len(rebuilt.overflow),
        )
        return rebuilt
