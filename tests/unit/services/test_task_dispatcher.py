import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from celery import Celery

from covenantiq.core import task_names
from covenantiq.core.enums import AnalysisJobType
from covenantiq.core.logging import bind_context, clear_context
from covenantiq.services.task_dispatcher import (
    CeleryTaskDispatcher,
    NullTaskDispatcher,
    QueueDepthProbe,
)
from covenantiq.workers import queues


@pytest.fixture
def app() -> Iterator[Celery]:
    """The real queue topology and routes on an in-memory broker, emptied for each test."""
    app = Celery("test", broker="memory://")
    app.conf.task_queues = queues.QUEUES
    app.conf.task_routes = queues.ROUTES
    app.conf.task_create_missing_queues = False
    app.conf.task_default_exchange = queues.EXCHANGE
    app.conf.task_default_queue = queues.DOCUMENT_PROCESSING
    queues.declare_topology(app)
    purge(app)
    yield app
    purge(app)


def purge(app: Celery) -> None:
    with app.connection_for_write() as connection:
        for name in queues.WORK_QUEUES:
            connection.default_channel.queue_purge(name)


def take(app: Celery, queue: str) -> Any:
    with app.connection_for_read() as connection:
        return connection.default_channel.basic_get(queue=queue, no_ack=True)


def depth(app: Celery, queue: str) -> int:
    with app.connection_for_read() as connection:
        return int(
            connection.default_channel.queue_declare(queue=queue, passive=True).message_count
        )


class TestPublishing:
    async def test_ingestion_goes_to_document_processing_with_only_ids(self, app: Celery) -> None:
        job_id = uuid4()
        await CeleryTaskDispatcher(app).enqueue_ingestion(job_id)

        message = take(app, "document_processing")
        assert message is not None
        assert message.headers["task"] == task_names.PROCESS_DOCUMENT
        args, kwargs, _ = message.payload
        assert args == [str(job_id)]  # an id, never a payload or file bytes
        assert set(kwargs) == {"request_id"}

    async def test_the_request_id_travels_with_the_task(self, app: Celery) -> None:
        bind_context(request_id="req-from-the-api")
        try:
            await CeleryTaskDispatcher(app).enqueue_ingestion(uuid4())
        finally:
            clear_context()
        _, kwargs, _ = take(app, "document_processing").payload
        assert kwargs == {"request_id": "req-from-the-api"}

    async def test_without_a_request_context_the_request_id_is_blank(self, app: Celery) -> None:
        await CeleryTaskDispatcher(app).enqueue_ingestion(uuid4())
        _, kwargs, _ = take(app, "document_processing").payload
        assert kwargs == {"request_id": ""}

    async def test_purge_shares_the_ingestion_queue(self, app: Celery) -> None:
        document_id = uuid4()
        await CeleryTaskDispatcher(app).enqueue_purge(document_id)
        message = take(app, "document_processing")
        assert message.headers["task"] == task_names.PURGE_DOCUMENT
        assert message.payload[0] == [str(document_id)]

    @pytest.mark.parametrize(
        ("job_type", "task", "queue"),
        [
            (
                AnalysisJobType.CONTRACT_ANALYSIS,
                task_names.RUN_CONTRACT_ANALYSIS,
                "contract_analysis",
            ),
            (
                AnalysisJobType.VENDOR_ASSESSMENT,
                task_names.RUN_VENDOR_ASSESSMENT,
                "vendor_analysis",
            ),
        ],
    )
    async def test_analysis_tasks_route_to_their_queues(
        self, app: Celery, job_type: AnalysisJobType, task: str, queue: str
    ) -> None:
        job_id = uuid4()
        await CeleryTaskDispatcher(app).enqueue_analysis(job_type, job_id)
        message = take(app, queue)
        assert message.headers["task"] == task
        assert message.payload[0] == [str(job_id)]
        for other in set(queues.WORK_QUEUES) - {queue}:
            assert depth(app, other) == 0

    async def test_republish_keeps_the_original_arguments_and_routes_again(
        self, app: Celery
    ) -> None:
        await CeleryTaskDispatcher(app).republish(
            task_names.EMBED_DOCUMENT, ["job-1"], {"request_id": "old-request"}
        )
        message = take(app, "embedding_generation")
        assert message.payload[0] == ["job-1"]
        assert message.payload[1] == {"request_id": "old-request"}

    async def test_messages_are_persistent_on_durable_queues(self, app: Celery) -> None:
        await CeleryTaskDispatcher(app).enqueue_ingestion(uuid4())
        message = take(app, "document_processing")
        assert message.properties["delivery_mode"] == 2

    async def test_a_dead_broker_raises_quickly_instead_of_hanging(self) -> None:
        dead = Celery("dead", broker="amqp://nobody:nope@127.0.0.1:1//")
        dead.conf.broker_connection_timeout = 1
        dead.conf.task_queues = queues.QUEUES
        dead.conf.task_routes = queues.ROUTES
        started = time.monotonic()
        with pytest.raises(Exception, match=r"."):
            await CeleryTaskDispatcher(dead).enqueue_ingestion(uuid4())
        assert time.monotonic() - started < 15


class TestNullDispatcher:
    async def test_does_nothing_and_never_raises(self) -> None:
        dispatcher = NullTaskDispatcher()
        await dispatcher.enqueue_ingestion(uuid4())
        await dispatcher.enqueue_purge(uuid4())
        await dispatcher.enqueue_analysis(AnalysisJobType.CONTRACT_ANALYSIS, uuid4())
        await dispatcher.republish("x", [], {})


class TestQueueDepthProbe:
    async def test_reports_the_number_of_waiting_messages(self, app: Celery) -> None:
        dispatcher = CeleryTaskDispatcher(app)
        probe = QueueDepthProbe(app, "document_processing", ttl_seconds=0)
        assert await probe.depth() == 0
        for _ in range(3):
            await dispatcher.enqueue_ingestion(uuid4())
        assert await probe.depth() == 3

    async def test_the_reading_is_cached_for_the_ttl(self, app: Celery) -> None:
        now = [100.0]
        probe = QueueDepthProbe(app, "document_processing", ttl_seconds=10, clock=lambda: now[0])
        dispatcher = CeleryTaskDispatcher(app)

        assert await probe.depth() == 0
        await dispatcher.enqueue_ingestion(uuid4())
        now[0] = 105.0  # inside the 10 s window
        assert await probe.depth() == 0
        now[0] = 111.0  # window over
        assert await probe.depth() == 1

    async def test_any_failure_reads_as_zero_so_it_never_blocks_uploads(self) -> None:
        dead = Celery("dead", broker="amqp://nobody:nope@127.0.0.1:1//")
        dead.conf.broker_connection_timeout = 1
        probe = QueueDepthProbe(dead, "document_processing")
        assert await probe.depth() == 0

    async def test_a_dead_broker_is_probed_once_per_window_not_once_per_upload(self) -> None:
        now = [0.0]
        dead = Celery("dead", broker="amqp://nobody:nope@127.0.0.1:1//")
        probe = QueueDepthProbe(dead, "document_processing", ttl_seconds=10, clock=lambda: now[0])
        attempts: list[int] = []

        def failing() -> int:
            attempts.append(1)
            raise ConnectionError("broker down")

        probe._read_depth = failing  # type: ignore[method-assign]
        for _ in range(5):
            assert await probe.depth() == 0
        assert len(attempts) == 1
        now[0] = 11.0
        assert await probe.depth() == 0
        assert len(attempts) == 2

    async def test_a_missing_queue_reads_as_zero(self, app: Celery) -> None:
        probe = QueueDepthProbe(app, "no_such_queue", ttl_seconds=0)
        assert await probe.depth() == 0
