"""The two retrieval caches (spec 19.14): query embeddings (24 h) and rerank scores (1 h).

Retrieval results themselves are never cached: documents change and ownership filters vary.

A cache is only an optimisation, so it must never make a request slower or fail it. Every call has
a short timeout; any failure reads as a miss; and after three failures in a row the cache is
skipped for 30 seconds, so a dead Redis costs three short timeouts, not two seconds per lookup.
Lookups and stores are batched (one round trip for all 30 rerank scores).
"""

import asyncio
import hashlib
from array import array
from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from covenantiq.ai.rerank.breaker import CircuitBreaker
from covenantiq.core.logging import get_logger

log = get_logger(__name__)

CACHE_TIMEOUT_SECONDS = 0.25


class Cache(Protocol):
    async def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        """One value (or None) per key, in order."""
        ...

    async def set_many(self, items: Mapping[str, bytes], ttl_seconds: int) -> None: ...


class NullCache:
    async def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        return [None] * len(keys)

    async def set_many(self, items: Mapping[str, bytes], ttl_seconds: int) -> None:
        return None


class RedisCache:
    """Redis, failing open and fast."""

    def __init__(
        self,
        redis: Redis,
        *,
        timeout_seconds: float = CACHE_TIMEOUT_SECONDS,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._redis = redis
        self._timeout = timeout_seconds
        self._breaker = breaker or CircuitBreaker(name="cache")

    async def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        if not keys or self._breaker.is_open():
            return [None] * len(keys)
        try:
            values = await asyncio.wait_for(self._redis.mget(list(keys)), self._timeout)
        except (RedisError, OSError, TimeoutError):
            self._breaker.record_failure()
            log.warning("cache.unavailable", op="get")
            return [None] * len(keys)
        self._breaker.record_success()
        return [
            None if value is None else value.encode() if isinstance(value, str) else bytes(value)
            for value in values
        ]

    async def set_many(self, items: Mapping[str, bytes], ttl_seconds: int) -> None:
        if not items or self._breaker.is_open():
            return
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for key, value in items.items():
                    pipe.set(key, value, ex=ttl_seconds)
                await asyncio.wait_for(pipe.execute(), self._timeout)
        except (RedisError, OSError, TimeoutError):
            self._breaker.record_failure()
            log.warning("cache.unavailable", op="set")
            return
        self._breaker.record_success()


def query_sha(query: str) -> str:
    """The query with whitespace normalised, hashed: never the text itself in a key or a log."""
    return hashlib.sha256(" ".join(query.split()).encode()).hexdigest()


def embedding_key(env: str, model: str, query: str) -> str:
    return f"ciq:{env}:cache:qemb:{model}:{query_sha(query)}"


def rerank_key(env: str, model: str, sha: str, chunk_id: UUID) -> str:
    return f"ciq:{env}:cache:rr:{model}:{sha}:{chunk_id}"


def encode_vector(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def decode_vector(raw: bytes) -> list[float]:
    values = array("f")
    values.frombytes(raw)
    return list(values)
