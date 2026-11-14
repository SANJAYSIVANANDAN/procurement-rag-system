"""CovenantTask behaviour, run through Celery's own eager execution so retries, state and Reject
are real, with a fake job service in place of the database."""

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError

from covenantiq.core.errors import PermanentTaskError, TransientError
from covenantiq.core.logging import bind_context
from covenantiq.workers import runtime
from covenantiq.workers.base_task import CovenantTask

JOB = "00000000-0000-7000-8000-000000000001"


class FakeJobs:
    def __init__(self) -> None:
        self.failed: list[tuple[str, str, str, str]] = []
        self.raise_on_mark = False
        self.released: list[tuple[str, str]] = []

    async def release(self, kind: str, job_id: UUID) -> bool:
        self.released.append((kind, str(job_id)))
        return True

    async def mark_failed(self, kind: str, job_id: UUID, *, code: str, message: str) -> bool:
        if self.raise_on_mark:
            raise RuntimeError("database is down")
        self.failed.append((kind, str(job_id), code, message))
        return True


class FakeContainer:
    def __init__(self) -> None:
        self.job_service = FakeJobs()

    async def aclose(self) -> None:
        return None


@pytest.fixture
def jobs() -> Iterator[FakeJobs]:
    container = FakeContainer()
    runtime.install_runtime(container, asyncio.new_event_loop())  # type: ignore[arg-type]
    yield container.job_service
    runtime.shutdown_runtime()


@pytest.fixture
def app() -> Celery:
    return Celery("test", broker="memory://", backend="cache+memory://")


def make_task(app: Celery, name: str, body: Any, **options: Any) -> Any:
    @app.task(bind=True, base=CovenantTask, name=name, **options)
    def task(self: CovenantTask, job_id: str, *, request_id: str = "") -> Any:
        return body(self, job_id)

    return task


@pytest.mark.usefixtures("jobs")
def test_success_returns_the_result(app: Celery) -> None:
    task = make_task(
        app, "t.ok", lambda self, job_id: {"done": job_id}, job_kind="document_processing"
    )
    result = task.apply(args=[JOB], kwargs={"request_id": "r"})
    assert result.state == "SUCCESS"
    assert result.result == {"done": JOB}


def test_permanent_error_marks_the_job_failed_then_rejects(app: Celery, jobs: FakeJobs) -> None:
    def body(self: Any, job_id: str) -> None:
        raise PermanentTaskError("bad input", job_code="EXTRACTION_FAILED")

    task = make_task(app, "t.perm", body, job_kind="document_processing")
    result = task.apply(args=[JOB])
    assert result.state == "REJECTED"  # Reject(requeue=False) is what sends it to the DLQ
    assert jobs.failed == [("document_processing", JOB, "EXTRACTION_FAILED", "bad input")]


def test_unexpected_error_is_a_permanent_failure_with_a_safe_message(
    app: Celery, jobs: FakeJobs
) -> None:
    def body(self: Any, job_id: str) -> None:
        raise RuntimeError("secret internal detail: password=hunter2")

    task = make_task(app, "t.boom", body, job_kind="document_processing")
    result = task.apply(args=[JOB])
    assert result.state == "REJECTED"
    ((_, _, code, message),) = jobs.failed
    assert code == "INTERNAL_ERROR"
    assert "hunter2" not in message and "secret" not in message


def test_a_transient_error_is_retried_until_it_succeeds(app: Celery, jobs: FakeJobs) -> None:
    attempts: list[int] = []

    def body(self: Any, job_id: str) -> dict[str, bool]:
        attempts.append(self.request.retries)
        if self.request.retries < 2:
            raise TransientError("try again")
        return {"ok": True}

    task = make_task(app, "t.flaky", body, job_kind="document_processing", max_retries=3)
    result = task.apply(args=[JOB])
    assert result.state == "SUCCESS"
    assert attempts == [0, 1, 2]
    assert jobs.failed == []  # a retried-then-successful task never touches the job as failed


def test_the_job_is_released_before_each_retry_so_the_retry_can_claim_it(
    app: Celery, jobs: FakeJobs
) -> None:
    def body(self: Any, job_id: str) -> dict[str, bool]:
        if self.request.retries < 2:
            raise TransientError("try again")
        return {"ok": True}

    task = make_task(app, "t.release", body, job_kind="document_processing", max_retries=3)
    assert task.apply(args=[JOB]).state == "SUCCESS"
    assert jobs.released == [("document_processing", JOB)] * 2


def test_a_timeout_retry_also_releases_the_job(app: Celery, jobs: FakeJobs) -> None:
    def body(self: Any, job_id: str) -> dict[str, bool]:
        if self.request.retries == 0:
            raise SoftTimeLimitExceeded()
        return {"ok": True}

    task = make_task(app, "t.slow", body, job_kind="document_processing", max_retries=3)
    assert task.apply(args=[JOB]).state == "SUCCESS"
    assert jobs.released == [("document_processing", JOB)]


def test_a_failure_to_release_does_not_stop_the_retry(app: Celery, jobs: FakeJobs) -> None:
    async def broken(kind: str, job_id: UUID) -> bool:
        raise RuntimeError("database is down")

    jobs.release = broken  # type: ignore[method-assign]

    def body(self: Any, job_id: str) -> dict[str, bool]:
        if self.request.retries == 0:
            raise TransientError("try again")
        return {"ok": True}

    task = make_task(app, "t.norelease", body, job_kind="document_processing", max_retries=3)
    assert task.apply(args=[JOB]).state == "SUCCESS"


def test_tasks_that_never_retry_do_not_release_anything(app: Celery, jobs: FakeJobs) -> None:
    def body(self: Any, job_id: str) -> None:
        raise PermanentTaskError("bad", job_code="X")

    task = make_task(app, "t.noretry", body, job_kind="document_processing")
    task.apply(args=[JOB])
    assert jobs.released == []


def test_exhausted_retries_fail_the_job_and_reject(app: Celery, jobs: FakeJobs) -> None:
    attempts: list[int] = []

    def body(self: Any, job_id: str) -> None:
        attempts.append(self.request.retries)
        raise TransientError("still down")

    task = make_task(app, "t.down", body, job_kind="document_processing", max_retries=2)
    result = task.apply(args=[JOB])
    assert result.state == "REJECTED"
    assert attempts == [0, 1, 2]  # the first try plus max_retries retries
    ((_, _, code, _),) = jobs.failed
    assert code == "TRANSIENT_FAILURE"


def test_database_connection_errors_count_as_transient(app: Celery, jobs: FakeJobs) -> None:
    attempts: list[int] = []

    def body(self: Any, job_id: str) -> dict[str, bool]:
        attempts.append(self.request.retries)
        if self.request.retries == 0:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        return {"ok": True}

    task = make_task(app, "t.db", body, job_kind="document_processing", max_retries=3)
    assert task.apply(args=[JOB]).state == "SUCCESS"
    assert attempts == [0, 1]


def test_a_timeout_is_retried_once_then_fails(app: Celery, jobs: FakeJobs) -> None:
    attempts: list[int] = []

    def body(self: Any, job_id: str) -> None:
        attempts.append(self.request.retries)
        raise SoftTimeLimitExceeded()

    task = make_task(app, "t.slow", body, job_kind="document_processing", max_retries=5)
    result = task.apply(args=[JOB])
    assert result.state == "REJECTED"
    assert attempts == [0, 1]  # once, as the spec says, however many retries are allowed
    ((_, _, code, _),) = jobs.failed
    assert code == "TIMEOUT"


def test_a_failure_to_record_the_failure_still_dead_letters(app: Celery, jobs: FakeJobs) -> None:
    jobs.raise_on_mark = True

    def body(self: Any, job_id: str) -> None:
        raise PermanentTaskError("x")

    task = make_task(app, "t.dbdown", body, job_kind="document_processing")
    assert task.apply(args=[JOB]).state == "REJECTED"


def test_tasks_without_a_job_do_not_touch_job_state(app: Celery, jobs: FakeJobs) -> None:
    def body(self: Any, job_id: str) -> None:
        raise PermanentTaskError("purge failed")

    task = make_task(app, "t.purge", body)  # no job_kind, like ingest.purge_document
    assert task.apply(args=["some-document-id"]).state == "REJECTED"
    assert jobs.failed == []


def test_analysis_jobs_are_reported_with_their_kind(app: Celery, jobs: FakeJobs) -> None:
    def body(self: Any, job_id: str) -> None:
        raise PermanentTaskError("nope", job_code="NOT_IMPLEMENTED")

    task = make_task(app, "t.analysis", body, job_kind="analysis")
    task.apply(args=[JOB])
    assert jobs.failed[0][0] == "analysis"


@pytest.mark.usefixtures("jobs")
def test_log_context_is_bound_during_the_task_and_cleared_after(
    app: Celery, capsys: pytest.CaptureFixture[str]
) -> None:
    from covenantiq.core.enums import LogFormat
    from covenantiq.core.logging import configure_logging, get_logger

    configure_logging("DEBUG", LogFormat.JSON)

    def body(self: Any, job_id: str) -> str:
        get_logger("t").info("inside")
        return "ok"

    task = make_task(app, "t.ctx", body, job_kind="document_processing")
    bind_context(user_id="u-outer")
    task.apply(args=[JOB], kwargs={"request_id": "req-77"})
    inside = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    record = next(r for r in inside if r.get("event") == "inside")
    assert record["job_id"] == JOB
    assert record["request_id"] == "req-77"
    assert record["task_id"]
    get_logger("t").info("after")
    after = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert "job_id" not in after and "request_id" not in after


class TestBackoff:
    def test_grows_exponentially_with_jitter_inside_bounds(self) -> None:
        task = CovenantTask()
        task.retry_backoff_base = 5.0
        task.retry_backoff_max = 300.0
        for retries, ceiling in [(0, 5), (1, 10), (2, 20), (3, 40)]:
            samples = [task.countdown(retries) for _ in range(200)]
            assert all(ceiling / 2 <= s <= ceiling for s in samples)
            assert max(samples) > ceiling * 0.8  # jitter actually spreads the values

    def test_is_capped(self) -> None:
        task = CovenantTask()
        task.retry_backoff_base = 5.0
        task.retry_backoff_max = 300.0
        assert all(150 <= task.countdown(20) <= 300 for _ in range(100))
