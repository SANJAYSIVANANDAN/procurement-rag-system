import asyncio
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol
from uuid import UUID

from celery import Celery

from covenantiq.core import task_names
from covenantiq.core.enums import AnalysisJobType
from covenantiq.core.logging import current_request_id, get_logger

log = get_logger(__name__)

ANALYSIS_TASK = {
    AnalysisJobType.CONTRACT_ANALYSIS: task_names.RUN_CONTRACT_ANALYSIS,
    AnalysisJobType.VENDOR_ASSESSMENT: task_names.RUN_VENDOR_ASSESSMENT,
}

# Fail fast when the broker is down: the caller marks the job failed and answers 503.
PUBLISH_RETRY_POLICY = {
    "max_retries": 1,
    "interval_start": 0,
    "interval_step": 0.2,
    "interval_max": 1,
}

DEPTH_CACHE_SECONDS = 10.0


class TaskDispatcher(Protocol):
    """Publishes background work. Always called after the commit that created the job row, so a
    worker never receives an id that does not exist (spec 15.8)."""

    async def enqueue_ingestion(self, job_id: UUID) -> None: ...

    async def enqueue_embedding(self, job_id: UUID) -> None: ...

    async def enqueue_purge(self, document_id: UUID) -> None: ...

    async def enqueue_analysis(self, job_type: AnalysisJobType, job_id: UUID) -> None: ...

    async def republish(self, task_name: str, args: Sequence[Any], kwargs: dict[str, Any]) -> None:
        """Publish a task exactly as it was originally sent (dead-letter replay)."""
        ...


class CeleryTaskDispatcher:
    """Publishes through Celery. Tasks carry only ids and the request id; routing to the right
    queue comes from the app's task_routes, so this class never names a queue."""

    def __init__(self, app: Celery) -> None:
        self._app = app

    async def enqueue_ingestion(self, job_id: UUID) -> None:
        await self._send(task_names.PROCESS_DOCUMENT, [str(job_id)])

    async def enqueue_embedding(self, job_id: UUID) -> None:
        await self._send(task_names.EMBED_DOCUMENT, [str(job_id)])

    async def enqueue_purge(self, document_id: UUID) -> None:
        await self._send(task_names.PURGE_DOCUMENT, [str(document_id)])

    async def enqueue_analysis(self, job_type: AnalysisJobType, job_id: UUID) -> None:
        await self._send(ANALYSIS_TASK[job_type], [str(job_id)])

    async def republish(self, task_name: str, args: Sequence[Any], kwargs: dict[str, Any]) -> None:
        await self._publish(task_name, list(args), kwargs)

    async def _send(self, task_name: str, args: list[Any]) -> None:
        await self._publish(task_name, args, {"request_id": current_request_id() or ""})

    async def _publish(self, task_name: str, args: list[Any], kwargs: dict[str, Any]) -> None:
        # send_task does blocking network I/O
        await asyncio.to_thread(
            self._app.send_task,
            task_name,
            args=args,
            kwargs=kwargs,
            retry=True,
            retry_policy=PUBLISH_RETRY_POLICY,
        )
        log.info("task.published", task=task_name)


class NullTaskDispatcher:
    """Used where no broker is wired: jobs simply stay `queued`."""

    async def enqueue_ingestion(self, job_id: UUID) -> None:
        log.info("dispatch.skipped", task=task_names.PROCESS_DOCUMENT, job_id=str(job_id))

    async def enqueue_embedding(self, job_id: UUID) -> None:
        log.info("dispatch.skipped", task=task_names.EMBED_DOCUMENT, job_id=str(job_id))

    async def enqueue_purge(self, document_id: UUID) -> None:
        log.info("dispatch.skipped", task=task_names.PURGE_DOCUMENT, document_id=str(document_id))

    async def enqueue_analysis(self, job_type: AnalysisJobType, job_id: UUID) -> None:
        log.info("dispatch.skipped", task=ANALYSIS_TASK[job_type], job_id=str(job_id))

    async def republish(self, task_name: str, args: Sequence[Any], kwargs: dict[str, Any]) -> None:
        log.info("dispatch.skipped", task=task_name)


class QueueDepthProbe:
    """How many messages are waiting in a queue (a passive declare), cached for a few seconds.

    Any failure reads as zero: a dead broker is reported by the dispatch itself, and the
    backpressure check must never be the reason an upload fails.
    """

    def __init__(
        self,
        app: Celery,
        queue: str,
        *,
        ttl_seconds: float = DEPTH_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._queue = queue
        self._ttl = ttl_seconds
        self._clock = clock
        self._cached: tuple[float, int] | None = None

    async def depth(self) -> int:
        now = self._clock()
        if self._cached is not None and now - self._cached[0] < self._ttl:
            return self._cached[1]
        try:
            depth = await asyncio.to_thread(self._read_depth)
        except Exception:
            log.warning("queue_depth.unavailable", queue=self._queue)
            depth = 0
        # Failures are cached too, so a dead broker costs one connection attempt per window, not
        # one per upload.
        self._cached = (now, depth)
        return depth

    def _read_depth(self) -> int:
        with self._app.connection_for_read() as connection:
            connection.ensure_connection(max_retries=0)  # a single attempt
            result = connection.default_channel.queue_declare(queue=self._queue, passive=True)
            return int(result.message_count)
