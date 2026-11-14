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
        self.values: dict[str, str] = {}
        self.down = down

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    async def set(
        self, name: str, value: str, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if self.down:
            raise RedisConnectionError("redis is down")
        if nx and name in self.values:
            return None
        self.values[name] = value
        if ex is not None:
            self.expiries[name] = ex
        return True

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


class RecordingDispatcher:
    """A TaskDispatcher that records what was published, and can be told to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.ingestion: list[Any] = []
        self.embedding: list[Any] = []
        self.purges: list[Any] = []
        self.analysis: list[Any] = []
        self.republished: list[Any] = []
        self.fail = fail

    async def enqueue_ingestion(self, job_id: Any) -> None:
        if self.fail:
            raise RuntimeError("broker is down")
        self.ingestion.append(job_id)

    async def enqueue_embedding(self, job_id: Any) -> None:
        if self.fail:
            raise RuntimeError("broker is down")
        self.embedding.append(job_id)

    async def enqueue_purge(self, document_id: Any) -> None:
        if self.fail:
            raise RuntimeError("broker is down")
        self.purges.append(document_id)

    async def enqueue_analysis(self, job_type: Any, job_id: Any) -> None:
        if self.fail:
            raise RuntimeError("broker is down")
        self.analysis.append((job_type, job_id))

    async def republish(self, task_name: str, args: Any, kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("broker is down")
        self.republished.append((task_name, list(args), dict(kwargs)))


class FakeExtractor:
    """A DocumentExtractor that returns a canned result (or raises), without Docling."""

    def __init__(
        self,
        result: Any = None,
        *,
        error: BaseException | None = None,
        progress_steps: int = 0,
    ) -> None:
        self.result = result if result is not None else healthy_result()
        self.error = error
        self.progress_steps = progress_steps
        self.calls: list[tuple[Any, str]] = []

    def extract(self, path: Any, extension: str, *, progress: Any = None) -> Any:
        self.calls.append((path, extension))
        for step in range(1, self.progress_steps + 1):
            if progress is not None:
                progress(step / self.progress_steps)
        if self.error is not None:
            raise self.error
        return self.result


def healthy_result(pages: int | None = 2, **overrides: Any) -> Any:
    from covenantiq.ingestion.extraction.quality import compute_quality
    from covenantiq.ingestion.extraction.structure import RawItem, build_blocks

    items = [
        RawItem("heading", "1. Definitions", 1),
        RawItem("text", "1.1 Agreement means this Master Services Agreement. " * 8, 1),
        RawItem("heading", "2. Payment", min(2, pages or 1)),
        RawItem(
            "text",
            "2.1 The Customer shall pay each invoice within thirty days. " * 8,
            min(2, pages or 1),
        ),
    ]
    items = overrides.pop("items", items)
    blocks, tables = build_blocks(items)
    from covenantiq.ingestion.extraction.types import ExtractionResult

    return ExtractionResult(
        blocks=blocks,
        page_count=pages,
        quality=compute_quality(blocks, tables=tables, page_count=pages),
    )


class InMemoryCache:
    """A rag `Cache` that remembers what was stored (and for how long), and can be made to fail."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self.lookups = 0  # calls to get_many, however many keys each asked for
        self.fail = False

    async def get_many(self, keys: Any) -> list[bytes | None]:
        self.lookups += 1
        return [None if self.fail else self.data.get(key) for key in keys]

    async def set_many(self, items: Any, ttl_seconds: int) -> None:
        if not self.fail:
            for key, value in items.items():
                self.data[key] = value
                self.ttls[key] = ttl_seconds


__all__: list[Any] = [
    "FakeExtractor",
    "FakePipeline",
    "FakeRedis",
    "InMemoryCache",
    "RecordingDispatcher",
    "fake_redis_keys",
    "healthy_result",
]
