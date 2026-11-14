import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from kombu.connection import Connection
from kombu.entity import Queue

from covenantiq.core import task_names
from covenantiq.core.logging import get_logger
from covenantiq.services.job_service import ANALYSIS, PROCESSING, JobKind, JobService
from covenantiq.services.task_dispatcher import TaskDispatcher
from covenantiq.workers.queues import WORK_QUEUES, dlq_name

log = get_logger(__name__)

JOB_KIND_BY_TASK: dict[str, JobKind | None] = {
    task_names.PROCESS_DOCUMENT: PROCESSING,
    task_names.EMBED_DOCUMENT: PROCESSING,
    task_names.PURGE_DOCUMENT: None,  # keyed by document id; there is no job row to reset
    task_names.RUN_CONTRACT_ANALYSIS: ANALYSIS,
    task_names.RUN_VENDOR_ASSESSMENT: ANALYSIS,
    task_names.RUN_VENDOR_COMPARISON: ANALYSIS,
}

DEFAULT_LIMIT = 100


@dataclass(frozen=True)
class DeadLetter:
    queue: str
    task: str
    task_id: str | None
    target_id: str | None
    request_id: str | None
    reason: str | None


@dataclass(frozen=True)
class ReplayResult:
    replayed: int
    skipped: int


class _Held:
    """A message fetched from a DLQ and not yet acknowledged, with its parsed content."""

    def __init__(self, message: Any) -> None:
        self.message = message
        headers = message.headers or {}
        body = message.payload
        args, kwargs = (body[0], body[1]) if isinstance(body, list | tuple) else ([], {})
        self.task: str = headers.get("task", "")
        self.task_id: str | None = headers.get("id")
        self.args: list[Any] = list(args)
        self.kwargs: dict[str, Any] = dict(kwargs)
        deaths = headers.get("x-death") or []
        self.reason: str | None = deaths[0].get("reason") if deaths else None

    @property
    def target_id(self) -> str | None:
        return str(self.args[0]) if self.args else None

    def describe(self, queue: str) -> DeadLetter:
        return DeadLetter(
            queue=queue,
            task=self.task,
            task_id=self.task_id,
            target_id=self.target_id,
            request_id=self.kwargs.get("request_id") or None,
            reason=self.reason,
        )


class DlqService:
    """Inspect and replay dead-lettered tasks (spec 25.6).

    Messages are fetched without acknowledgement, so anything not explicitly replayed returns to
    the queue untouched when the connection closes.
    """

    def __init__(
        self,
        connection_factory: Callable[[], Connection],
        jobs: JobService,
        dispatcher: TaskDispatcher,
    ) -> None:
        self._connection_factory = connection_factory
        self._jobs = jobs
        self._dispatcher = dispatcher

    @staticmethod
    def check_queue(queue: str) -> str:
        if queue not in WORK_QUEUES:
            raise ValueError(f"unknown queue {queue!r}; choose one of {', '.join(WORK_QUEUES)}")
        return queue

    async def peek(self, queue: str, *, limit: int = DEFAULT_LIMIT) -> list[DeadLetter]:
        queue = self.check_queue(queue)
        connection = self._connection_factory()
        try:
            held = await asyncio.to_thread(self._fetch, connection, queue, limit)
            described = [item.describe(queue) for item in held]
            await asyncio.to_thread(self._release, held)
            return described
        finally:
            await asyncio.to_thread(connection.release)

    async def replay(
        self, queue: str, *, job_id: UUID | None = None, limit: int = DEFAULT_LIMIT
    ) -> ReplayResult:
        """Reset each failed job to `queued` (compare-and-set from `failed`), publish its task
        again, and only then remove the message from the DLQ."""
        queue = self.check_queue(queue)
        wanted = str(job_id) if job_id is not None else None
        connection = self._connection_factory()
        replayed = skipped = 0
        try:
            held = await asyncio.to_thread(self._fetch, connection, queue, limit)
            leftover: list[_Held] = []
            for item in held:
                if wanted is not None and item.target_id != wanted:
                    leftover.append(item)
                    continue
                if await self._replay_one(item):
                    await asyncio.to_thread(item.message.ack)
                    replayed += 1
                else:
                    leftover.append(item)
                    skipped += 1
            await asyncio.to_thread(self._release, leftover)
        finally:
            await asyncio.to_thread(connection.release)
        return ReplayResult(replayed=replayed, skipped=skipped)

    async def _replay_one(self, item: _Held) -> bool:
        kind = JOB_KIND_BY_TASK.get(item.task)
        if item.task not in JOB_KIND_BY_TASK:
            log.warning("dlq.unknown_task", task=item.task)
            return False
        if kind is not None:
            if item.target_id is None or not await self._jobs.reset_for_replay(
                kind, UUID(item.target_id)
            ):
                return False  # the job is not failed (already replayed, or never failed)
        try:
            await self._dispatcher.republish(item.task, item.args, item.kwargs)
        except Exception:
            log.error("dlq.republish_failed", task=item.task, target=item.target_id)
            return False
        return True

    @staticmethod
    def _fetch(connection: Connection, queue: str, limit: int) -> list[_Held]:
        # Through a bound Queue, so the message is a kombu Message (payload, ack, requeue) on
        # every transport. A bare channel returns a raw AMQP message on RabbitMQ.
        bound = Queue(dlq_name(queue), no_declare=True).bind(connection.default_channel)
        held: list[_Held] = []
        while len(held) < limit:
            message = bound.get(no_ack=False)
            if message is None:
                break
            held.append(_Held(message))
        return held

    @staticmethod
    def _release(held: list[_Held]) -> None:
        for item in held:
            item.message.requeue()
