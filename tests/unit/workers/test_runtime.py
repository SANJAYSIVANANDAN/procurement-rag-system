import asyncio
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest

from covenantiq.workers import runtime
from covenantiq.workers.heartbeat import heartbeat


class FakeContainer:
    closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def installed() -> Iterator[FakeContainer]:
    container = FakeContainer()
    runtime.install_runtime(container, asyncio.new_event_loop())  # type: ignore[arg-type]
    yield container
    runtime.shutdown_runtime()


def test_run_async_returns_the_coroutine_result(installed: FakeContainer) -> None:
    async def work() -> int:
        await asyncio.sleep(0)
        return 42

    assert runtime.run_async(work()) == 42


def test_run_async_propagates_exceptions(installed: FakeContainer) -> None:
    async def work() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        runtime.run_async(work())


def test_the_same_loop_is_reused_across_calls(installed: FakeContainer) -> None:
    async def current_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    assert runtime.run_async(current_loop()) is runtime.run_async(current_loop())


def test_an_interrupted_call_is_unwound_and_leaves_no_stale_work(
    installed: FakeContainer,
) -> None:
    """A soft time limit raises inside the loop while a task is mid-flight. The half-finished
    coroutine must be cancelled and unwound, not left to resume during the next task."""
    events: list[str] = []

    async def long_running() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            events.append("unwound")

    def interrupt() -> None:
        raise KeyboardInterrupt  # what Celery's soft-limit signal handler does to the main thread

    assert runtime._loop is not None
    runtime._loop.call_later(0.05, interrupt)
    with pytest.raises(KeyboardInterrupt):
        runtime.run_async(long_running())

    assert events == ["unwound"]
    assert runtime.run_async(asyncio.sleep(0, result="next task")) == "next task"


def test_run_async_requires_an_initialised_runtime() -> None:
    runtime.shutdown_runtime()

    async def work() -> None:
        return None

    with pytest.raises(RuntimeError, match="not been initialised"):
        runtime.run_async(work())
    with pytest.raises(RuntimeError, match="not been initialised"):
        runtime.container()


def test_shutdown_closes_the_container() -> None:
    container = FakeContainer()
    runtime.install_runtime(container, asyncio.new_event_loop())  # type: ignore[arg-type]
    runtime.shutdown_runtime()
    assert container.closed


class RecordingJobs:
    def __init__(self, *, fail: bool = False) -> None:
        self.touches: list[tuple[str, Any]] = []
        self.fail = fail

    async def touch(self, kind: str, job_id: Any, **_kw: Any) -> bool:
        if self.fail:
            raise RuntimeError("db down")
        self.touches.append((kind, job_id))
        return True


async def test_heartbeat_touches_the_job_periodically_until_the_block_ends() -> None:
    jobs = RecordingJobs()
    job_id = uuid4()
    async with heartbeat(jobs, "document_processing", job_id, interval=0.02):  # type: ignore[arg-type]
        await asyncio.sleep(0.15)
    seen = len(jobs.touches)
    assert seen >= 3
    assert all(t == ("document_processing", job_id) for t in jobs.touches)
    await asyncio.sleep(0.1)
    assert len(jobs.touches) == seen  # stopped when the block exited


async def test_heartbeat_survives_database_errors() -> None:
    jobs = RecordingJobs(fail=True)
    async with heartbeat(jobs, "analysis", uuid4(), interval=0.01):  # type: ignore[arg-type]
        await asyncio.sleep(0.05)
