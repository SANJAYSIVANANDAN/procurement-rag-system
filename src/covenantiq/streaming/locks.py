"""The lock that lets one reply at a time be generated in a conversation (spec 26.3, 26.5).

    SET key token NX EX ttl                    acquire; the token is random per holder
    Lua: delete only if the token matches      release (a late release never frees another's lock)
    Lua: expire only if the token matches      heartbeat, while a long reply is still being written

If the holder dies, the lock expires on its own after `ttl_seconds`.
"""

import secrets
from uuid import UUID

from redis.asyncio import Redis

from covenantiq.core import redis_keys

_RELEASE = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
    "else return 0 end"
)
_EXTEND = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) "
    "else return 0 end"
)


class ConversationLock:
    def __init__(self, redis: Redis, *, env: str, ttl_seconds: int = 120) -> None:
        self._redis = redis
        self._env = env
        self.ttl_seconds = ttl_seconds

    def _key(self, conversation_id: UUID) -> str:
        return redis_keys.conversation_lock(self._env, conversation_id)

    async def acquire(self, conversation_id: UUID) -> str | None:
        """The holder's token, or None if somebody else holds the lock. Redis errors propagate."""
        token = secrets.token_hex(16)
        taken = await self._redis.set(
            self._key(conversation_id), token, nx=True, ex=self.ttl_seconds
        )
        return token if taken else None

    async def extend(self, conversation_id: UUID, token: str) -> bool:
        result = await self._redis.eval(
            _EXTEND, 1, self._key(conversation_id), token, str(self.ttl_seconds)
        )
        return bool(result)

    async def release(self, conversation_id: UUID, token: str) -> bool:
        result = await self._redis.eval(_RELEASE, 1, self._key(conversation_id), token)
        return bool(result)
