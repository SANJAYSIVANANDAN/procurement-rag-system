import asyncio
import time
from typing import Any
from uuid import UUID

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from covenantiq.ai.rerank.breaker import FAILURES_BEFORE_OPENING, OPEN_FOR_SECONDS, CircuitBreaker
from covenantiq.rag.cache import (
    NullCache,
    RedisCache,
    decode_vector,
    embedding_key,
    encode_vector,
    query_sha,
    rerank_key,
)


class StubPipeline:
    def __init__(self, redis: "StubRedis") -> None:
        self.redis = redis
        self.queued: list[tuple[str, bytes, int]] = []

    async def __aenter__(self) -> "StubPipeline":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def set(self, key: str, value: bytes, ex: int) -> None:
        self.queued.append((key, value, ex))

    async def execute(self) -> None:
        await self.redis.behave()
        for key, value, ex in self.queued:
            self.redis.data[key] = value
            self.redis.ttls[key] = ex


class StubRedis:
    """Just enough of redis.asyncio.Redis: mget and a pipeline, with a switchable misbehaviour."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self.mode = "ok"  # ok | down | slow
        self.calls = 0

    async def behave(self) -> None:
        self.calls += 1
        if self.mode == "down":
            raise RedisConnectionError("refused")
        if self.mode == "slow":
            await asyncio.sleep(5)

    async def mget(self, keys: list[str]) -> list[Any]:
        await self.behave()
        return [self.data.get(key) for key in keys]

    def pipeline(self, transaction: bool = True) -> StubPipeline:
        return StubPipeline(self)


def cache(redis: StubRedis, clock: list[float] | None = None) -> RedisCache:
    now = clock if clock is not None else [0.0]
    return RedisCache(
        redis,  # type: ignore[arg-type]
        timeout_seconds=0.05,
        breaker=CircuitBreaker(clock=lambda: now[0]),
    )


class TestRedisCache:
    async def test_values_round_trip_in_key_order_with_misses_as_none(self) -> None:
        redis = StubRedis()
        store = cache(redis)
        await store.set_many({"a": b"1", "c": b"3"}, 60)
        assert await store.get_many(["a", "b", "c"]) == [b"1", None, b"3"]
        assert redis.ttls == {"a": 60, "c": 60}

    async def test_many_keys_cost_one_round_trip(self) -> None:
        redis = StubRedis()
        store = cache(redis)
        await store.set_many({f"k{i}": b"x" for i in range(30)}, 60)
        redis.calls = 0
        await store.get_many([f"k{i}" for i in range(30)])
        assert redis.calls == 1

    async def test_nothing_to_look_up_or_store_makes_no_call(self) -> None:
        redis = StubRedis()
        store = cache(redis)
        assert await store.get_many([]) == []
        await store.set_many({}, 60)
        assert redis.calls == 0

    async def test_string_values_are_returned_as_bytes(self) -> None:
        redis = StubRedis()
        redis.data["k"] = "0.5"  # type: ignore[assignment]
        assert await cache(redis).get_many(["k"]) == [b"0.5"]

    async def test_a_redis_that_is_down_is_a_miss_not_an_error(self) -> None:
        redis = StubRedis()
        redis.mode = "down"
        store = cache(redis)
        assert await store.get_many(["a", "b"]) == [None, None]
        await store.set_many({"a": b"1"}, 60)  # must not raise

    async def test_a_slow_redis_is_cut_off_quickly(self) -> None:
        redis = StubRedis()
        redis.mode = "slow"
        started = time.monotonic()
        assert await cache(redis).get_many(["a"]) == [None]
        assert time.monotonic() - started < 1.0  # the call would have taken 5 s

    async def test_repeated_failures_stop_the_calls_for_a_while(self) -> None:
        redis = StubRedis()
        redis.mode = "down"
        store = cache(redis)
        for _ in range(FAILURES_BEFORE_OPENING + 10):
            await store.get_many(["a"])
        assert redis.calls == FAILURES_BEFORE_OPENING  # the rest never reached Redis

    async def test_it_tries_again_after_the_pause_and_recovers(self) -> None:
        clock = [0.0]
        redis = StubRedis()
        redis.mode = "down"
        store = cache(redis, clock)
        for _ in range(FAILURES_BEFORE_OPENING):
            await store.get_many(["a"])
        redis.mode = "ok"
        redis.data["a"] = b"hit"
        assert await store.get_many(["a"]) == [None]  # still paused
        clock[0] = OPEN_FOR_SECONDS + 1
        assert await store.get_many(["a"]) == [b"hit"]


class TestNullCache:
    async def test_it_never_hits(self) -> None:
        null = NullCache()
        await null.set_many({"a": b"1"}, 60)
        assert await null.get_many(["a", "b"]) == [None, None]


class TestKeysAndVectors:
    def test_the_query_is_hashed_after_whitespace_is_normalised(self) -> None:
        assert query_sha("  total   liability ") == query_sha("total liability")
        assert query_sha("total liability") != query_sha("Total liability")
        assert len(query_sha("x")) == 64

    def test_keys_follow_the_spec_layout_and_never_contain_the_query(self) -> None:
        key = embedding_key("prod", "voyage-law-2", "secret question about indemnity")
        assert key.startswith("ciq:prod:cache:qemb:voyage-law-2:")
        assert "secret" not in key and "indemnity" not in key
        chunk = UUID(int=7)
        assert rerank_key("prod", "rerank-v5.0-fast", "abc", chunk) == (
            f"ciq:prod:cache:rr:rerank-v5.0-fast:abc:{chunk}"
        )

    def test_vectors_round_trip_as_float32(self) -> None:
        vector = [0.25, -1.5, 3.0, 0.1]
        decoded = decode_vector(encode_vector(vector))
        assert decoded[:3] == vector[:3]
        assert decoded[3] == pytest.approx(0.1, abs=1e-6)  # float32 precision
        assert len(encode_vector([0.0] * 1024)) == 4096
