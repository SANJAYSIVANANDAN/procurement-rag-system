from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError


class FakePipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._operations: list[tuple[str, str, int | None]] = []

    def incr(self, key: str) -> "FakePipeline":
        self._operations.append(("incr", key, None))
        return self

    def expire(self, key: str, seconds: int) -> "FakePipeline":
        self._operations.append(("expire", key, seconds))
        return self

    async def execute(self) -> list[int | bool]:
        if self._redis.down:
            raise RedisConnectionError("redis is down")
        results: list[int | bool] = []
        for name, key, argument in self._operations:
            if name == "incr":
                self._redis.counters[key] = self._redis.counters.get(key, 0) + 1
                results.append(self._redis.counters[key])
            else:
                assert argument is not None
                self._redis.expiries[key] = argument
                results.append(True)
        return results

    async def __aenter__(self) -> "FakePipeline":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class FakeRedis:
    """Just enough of redis.asyncio.Redis for the rate limiter and the readiness check."""

    def __init__(self, *, down: bool = False) -> None:
        self.counters: dict[str, int] = {}
        self.expiries: dict[str, int] = {}
        self.down = down

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    async def ping(self) -> bool:
        if self.down:
            raise RedisConnectionError("redis is down")
        return True

    async def aclose(self) -> None:
        return None

    def as_redis(self) -> Redis:
        return cast(Redis, self)


def fake_redis_keys(redis: FakeRedis) -> list[str]:
    return sorted(redis.counters)


__all__: list[Any] = ["FakePipeline", "FakeRedis", "fake_redis_keys"]
