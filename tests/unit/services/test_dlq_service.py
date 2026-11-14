from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from celery import Celery
from kombu import Producer

from covenantiq.core import task_names
from covenantiq.services.dlq_service import DlqService
from covenantiq.workers import queues
from tests.fakes import RecordingDispatcher

DLQ = "document_processing.dlq"


class FakeJobs:
    """Records resets; `resettable` is the set of job ids that are currently failed."""

    def __init__(self) -> None:
        self.resettable: set[str] = set()
        self.resets: list[tuple[str, str]] = []

    async def reset_for_replay(self, kind: str, job_id: UUID) -> bool:
        if str(job_id) not in self.resettable:
            return False
        self.resettable.discard(str(job_id))
        self.resets.append((kind, str(job_id)))
        return True


@pytest.fixture
def app() -> Iterator[Celery]:
    app = Celery("test", broker="memory://")
    queues.declare_topology(app)
    purge(app)
    yield app
    purge(app)


def purge(app: Celery) -> None:
    with app.connection_for_write() as connection:
        for name in queues.WORK_QUEUES:
            connection.default_channel.queue_purge(queues.dlq_name(name))


def dead_letter(
    app: Celery,
    task: str,
    target: str,
    *,
    queue: str = "document_processing",
    reason: str = "rejected",
    request_id: str = "req-1",
) -> None:
    with app.connection_for_write() as connection:
        Producer(connection.default_channel).publish(
            body=[[target], {"request_id": request_id}, {}],
            headers={"task": task, "id": f"task-{target}", "x-death": [{"reason": reason}]},
            exchange=queues.DEAD_LETTER_EXCHANGE,
            routing_key=queues.dlq_name(queue),
            serializer="json",
        )


def dlq_depth(app: Celery, queue: str = "document_processing") -> int:
    with app.connection_for_read() as connection:
        declared = connection.default_channel.queue_declare(
            queue=queues.dlq_name(queue), passive=True
        )
        return int(declared.message_count)


@pytest.fixture
def jobs() -> FakeJobs:
    return FakeJobs()


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def service(app: Celery, jobs: FakeJobs, dispatcher: RecordingDispatcher) -> DlqService:
    return DlqService(app.connection_for_write, jobs, dispatcher)  # type: ignore[arg-type]


class TestPeek:
    async def test_describes_each_message(self, app: Celery, service: DlqService) -> None:
        job = str(uuid4())
        dead_letter(app, task_names.PROCESS_DOCUMENT, job, reason="rejected", request_id="req-9")
        (letter,) = await service.peek("document_processing")
        assert letter.queue == "document_processing"
        assert letter.task == task_names.PROCESS_DOCUMENT
        assert letter.target_id == job
        assert letter.task_id == f"task-{job}"
        assert letter.request_id == "req-9"
        assert letter.reason == "rejected"

    async def test_peeking_removes_nothing(self, app: Celery, service: DlqService) -> None:
        for _ in range(3):
            dead_letter(app, task_names.PROCESS_DOCUMENT, str(uuid4()))
        assert len(await service.peek("document_processing")) == 3
        assert dlq_depth(app) == 3
        assert len(await service.peek("document_processing")) == 3  # still all there

    async def test_an_empty_dlq_lists_nothing(self, service: DlqService) -> None:
        assert await service.peek("contract_analysis") == []

    async def test_limit_caps_the_listing(self, app: Celery, service: DlqService) -> None:
        for _ in range(5):
            dead_letter(app, task_names.PROCESS_DOCUMENT, str(uuid4()))
        assert len(await service.peek("document_processing", limit=2)) == 2
        assert dlq_depth(app) == 5

    async def test_each_queue_has_its_own_dlq(self, app: Celery, service: DlqService) -> None:
        dead_letter(app, task_names.RUN_CONTRACT_ANALYSIS, str(uuid4()), queue="contract_analysis")
        assert await service.peek("document_processing") == []
        assert len(await service.peek("contract_analysis")) == 1

    async def test_unknown_queues_are_refused(self, service: DlqService) -> None:
        with pytest.raises(ValueError, match="unknown queue"):
            await service.peek("not_a_queue")


class TestReplay:
    async def test_a_failed_job_is_reset_republished_and_removed_from_the_dlq(
        self,
        app: Celery,
        service: DlqService,
        jobs: FakeJobs,
        dispatcher: RecordingDispatcher,
    ) -> None:
        job = str(uuid4())
        jobs.resettable.add(job)
        dead_letter(app, task_names.PROCESS_DOCUMENT, job, request_id="old-req")

        result = await service.replay("document_processing")

        assert (result.replayed, result.skipped) == (1, 0)
        assert jobs.resets == [("document_processing", job)]
        assert dispatcher.republished == [
            (task_names.PROCESS_DOCUMENT, [job], {"request_id": "old-req"})
        ]
        assert dlq_depth(app) == 0

    async def test_a_job_that_is_not_failed_is_skipped_and_left_in_the_dlq(
        self, app: Celery, service: DlqService, dispatcher: RecordingDispatcher
    ) -> None:
        dead_letter(app, task_names.PROCESS_DOCUMENT, str(uuid4()))  # job is not resettable
        result = await service.replay("document_processing")
        assert (result.replayed, result.skipped) == (0, 1)
        assert dispatcher.republished == []
        assert dlq_depth(app) == 1

    async def test_job_id_filter_replays_only_that_job(
        self, app: Celery, service: DlqService, jobs: FakeJobs, dispatcher: RecordingDispatcher
    ) -> None:
        wanted, other = str(uuid4()), str(uuid4())
        jobs.resettable.update({wanted, other})
        dead_letter(app, task_names.PROCESS_DOCUMENT, other)
        dead_letter(app, task_names.PROCESS_DOCUMENT, wanted)

        result = await service.replay("document_processing", job_id=UUID(wanted))

        assert (result.replayed, result.skipped) == (1, 0)
        assert [r[1] for r in dispatcher.republished] == [[wanted]]
        assert jobs.resets == [("document_processing", wanted)]
        assert dlq_depth(app) == 1  # the other job's message is untouched
        (remaining,) = await service.peek("document_processing")
        assert remaining.target_id == other

    async def test_a_publish_failure_keeps_the_message_in_the_dlq(
        self, app: Celery, service: DlqService, jobs: FakeJobs, dispatcher: RecordingDispatcher
    ) -> None:
        job = str(uuid4())
        jobs.resettable.add(job)
        dead_letter(app, task_names.PROCESS_DOCUMENT, job)
        dispatcher.fail = True

        result = await service.replay("document_processing")

        assert (result.replayed, result.skipped) == (0, 1)
        assert dlq_depth(app) == 1

    async def test_purge_tasks_have_no_job_to_reset(
        self, app: Celery, service: DlqService, jobs: FakeJobs, dispatcher: RecordingDispatcher
    ) -> None:
        dead_letter(app, task_names.PURGE_DOCUMENT, "some-document-id")
        result = await service.replay("document_processing")
        assert result.replayed == 1
        assert jobs.resets == []
        assert dispatcher.republished[0][0] == task_names.PURGE_DOCUMENT

    async def test_analysis_tasks_reset_analysis_jobs(
        self, app: Celery, service: DlqService, jobs: FakeJobs
    ) -> None:
        job = str(uuid4())
        jobs.resettable.add(job)
        dead_letter(app, task_names.RUN_CONTRACT_ANALYSIS, job, queue="contract_analysis")
        await service.replay("contract_analysis")
        assert jobs.resets == [("analysis", job)]

    async def test_unknown_tasks_are_never_replayed(
        self, app: Celery, service: DlqService, dispatcher: RecordingDispatcher
    ) -> None:
        dead_letter(app, "something.else", str(uuid4()))
        result = await service.replay("document_processing")
        assert (result.replayed, result.skipped) == (0, 1)
        assert dispatcher.republished == []
        assert dlq_depth(app) == 1

    async def test_replaying_twice_does_not_run_a_job_twice(
        self, app: Celery, service: DlqService, jobs: FakeJobs, dispatcher: RecordingDispatcher
    ) -> None:
        job = str(uuid4())
        jobs.resettable.add(job)
        dead_letter(app, task_names.PROCESS_DOCUMENT, job)
        dead_letter(app, task_names.PROCESS_DOCUMENT, job)  # the same job dead-lettered twice

        result = await service.replay("document_processing")

        assert (result.replayed, result.skipped) == (1, 1)  # the reset succeeds only once
        assert len(dispatcher.republished) == 1

    async def test_unknown_queues_are_refused(self, service: DlqService) -> None:
        with pytest.raises(ValueError, match="unknown queue"):
            await service.replay("not_a_queue")


def test_job_kind_table_covers_every_task() -> None:
    from covenantiq.services.dlq_service import JOB_KIND_BY_TASK

    expected: dict[str, Any] = {
        task_names.PROCESS_DOCUMENT,
        task_names.EMBED_DOCUMENT,
        task_names.PURGE_DOCUMENT,
        task_names.RUN_CONTRACT_ANALYSIS,
        task_names.RUN_VENDOR_ASSESSMENT,
        task_names.RUN_VENDOR_COMPARISON,
    }
    assert set(JOB_KIND_BY_TASK) == expected
