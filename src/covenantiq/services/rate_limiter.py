import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from covenantiq.core.errors import RateLimited
from covenantiq.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class RateLimit:
    limit: int
    window_seconds: int


# Spec 30.10. CIQ_RATE_LIMITS can override the counts (not the windows).
DEFAULT_LIMITS: dict[str, RateLimit] = {
    "auth_login_ip": RateLimit(10, 60),
    "auth_login_email": RateLimit(5, 60),
    "chat_submit": RateLimit(20, 60),
    "upload": RateLimit(30, 3600),
    "analysis_start": RateLimit(20, 3600),
    "default": RateLimit(300, 60),
}


class RateLimiter:
    """Fixed-window counters in Redis: INCR on a key that names the current window.

    The counter and its expiry are set in one MULTI/EXEC, so a key can never be left without a
    TTL. If Redis is unavailable the limiter fails open (logged), because refusing every login
    when Redis blips is worse than briefly not counting.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        env: str,
        overrides: Mapping[str, int] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._env = env
        self._clock = clock
        self._limits = {
            bucket: RateLimit((overrides or {}).get(bucket, rule.limit), rule.window_seconds)
            for bucket, rule in DEFAULT_LIMITS.items()
        }

    async def hit(self, bucket: str, identifier: str) -> None:
        """Count one request; raises RateLimited once the bucket's limit is exceeded."""
        rule = self._limits[bucket]
        now = int(self._clock())
        window_index, elapsed = divmod(now, rule.window_seconds)
        key = f"ciq:{self._env}:rl:{bucket}:{identifier}:{window_index}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, rule.window_seconds)
                count = int((await pipe.execute())[0])
        except (RedisError, OSError):
            log.warning("rate_limit.unavailable", bucket=bucket)
            return
        if count > rule.limit:
            raise RateLimited(
                "Too many requests. Try again later.",
                retry_after=rule.window_seconds - elapsed,
                details={"bucket": bucket},
            )
