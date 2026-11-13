import pytest

from covenantiq.core.errors import RateLimited
from covenantiq.services.rate_limiter import DEFAULT_LIMITS, RateLimiter
from tests.fakes import FakeRedis


def limiter(redis: FakeRedis, *, now: float = 1_000_020.0, **kwargs: object) -> RateLimiter:
    return RateLimiter(redis.as_redis(), env="test", clock=lambda: now, **kwargs)  # type: ignore[arg-type]


async def test_allows_up_to_the_limit_then_blocks() -> None:
    redis = FakeRedis()
    rl = limiter(redis)
    for _ in range(5):
        await rl.hit("auth_login_email", "a")
    with pytest.raises(RateLimited) as exc:
        await rl.hit("auth_login_email", "a")
    assert exc.value.details == {"bucket": "auth_login_email"}


async def test_retry_after_is_the_time_left_in_the_window() -> None:
    redis = FakeRedis()
    rl = limiter(redis, now=1_000_020.0)  # 1_000_020 % 60 == 0 -> a fresh window
    for _ in range(5):
        await rl.hit("auth_login_email", "a")
    with pytest.raises(RateLimited) as exc:
        await rl.hit("auth_login_email", "a")
    assert exc.value.retry_after == 60

    late = limiter(redis, now=1_000_020.0 + 45)  # same window, 45 s in
    with pytest.raises(RateLimited) as exc:
        await late.hit("auth_login_email", "a")
    assert exc.value.retry_after == 15


async def test_a_new_window_starts_a_new_count() -> None:
    redis = FakeRedis()
    for _ in range(5):
        await limiter(redis, now=1_000_020.0).hit("auth_login_email", "a")
    await limiter(redis, now=1_000_020.0 + 60).hit("auth_login_email", "a")


async def test_identifiers_and_buckets_are_independent() -> None:
    redis = FakeRedis()
    rl = limiter(redis)
    for _ in range(5):
        await rl.hit("auth_login_email", "a")
    await rl.hit("auth_login_email", "b")
    await rl.hit("auth_login_ip", "a")


async def test_keys_are_namespaced_and_always_expire() -> None:
    redis = FakeRedis()
    await limiter(redis).hit("upload", "user-1")
    (key,) = redis.counters
    assert key.startswith("ciq:test:rl:upload:user-1:")
    assert redis.expiries[key] == DEFAULT_LIMITS["upload"].window_seconds


async def test_overrides_change_the_limit() -> None:
    redis = FakeRedis()
    rl = limiter(redis, overrides={"auth_login_email": 2})
    await rl.hit("auth_login_email", "a")
    await rl.hit("auth_login_email", "a")
    with pytest.raises(RateLimited):
        await rl.hit("auth_login_email", "a")


async def test_fails_open_when_redis_is_down() -> None:
    rl = limiter(FakeRedis(down=True))
    for _ in range(50):
        await rl.hit("auth_login_email", "a")


def test_defaults_match_the_spec() -> None:
    assert {k: (v.limit, v.window_seconds) for k, v in DEFAULT_LIMITS.items()} == {
        "auth_login_ip": (10, 60),
        "auth_login_email": (5, 60),
        "chat_submit": (20, 60),
        "upload": (30, 3600),
        "analysis_start": (20, 3600),
        "default": (300, 60),
    }
