from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text

from covenantiq.api.container import Container
from covenantiq.db.uow import UnitOfWork
from tests.fakes import FakeRedis, RecordingDispatcher
from tests.fixtures import contracts as fixtures
from tests.integration.api.conftest import Backlog

MakeSession = Callable[[], Any]


def stored_files(root: Any) -> list[Any]:
    return [p for p in root.rglob("*") if p.is_file()]


UPLOAD = "/api/v1/documents/upload"


def pdf(seed: bytes = b"") -> bytes:
    return fixtures.make_pdf() + b"\n% " + (seed or uuid4().hex.encode())


async def upload(
    client: httpx.AsyncClient, session: Any, data: bytes | None = None
) -> httpx.Response:
    return await client.post(
        UPLOAD, headers=session.headers, files={"file": ("a.pdf", data or pdf())}
    )


async def age(uow_factory: Callable[[], UnitOfWork], job_id: str, minutes: int) -> None:
    async with uow_factory() as uow:
        await uow.session.execute(
            text(
                "UPDATE document_processing_jobs "
                "SET created_at = now() - make_interval(mins => :m) WHERE id = :id"
            ),
            {"m": minutes, "id": UUID(job_id)},
        )
        await uow.commit()


class TestBackpressure:
    async def test_uploads_are_refused_while_the_ingest_queue_is_too_deep(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        backlog: Backlog,
        storage_root: Any,
    ) -> None:
        session = await make_session()
        backlog.depth = 201  # the limit is 200
        response = await upload(client, session)
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "INGESTION_BACKLOG"
        assert int(response.headers["Retry-After"]) > 0
        assert not stored_files(storage_root)

    async def test_uploads_resume_when_the_queue_drains_and_200_is_still_accepted(
        self, client: httpx.AsyncClient, make_session: MakeSession, backlog: Backlog
    ) -> None:
        session = await make_session()
        backlog.depth = 200
        assert (await upload(client, session)).status_code == 202
        backlog.depth = 5000
        assert (await upload(client, session)).status_code == 429
        backlog.depth = 0
        assert (await upload(client, session)).status_code == 202

    async def test_other_endpoints_are_not_affected_by_the_backlog(
        self, client: httpx.AsyncClient, make_session: MakeSession, backlog: Backlog
    ) -> None:
        session = await make_session()
        backlog.depth = 10_000
        assert (await client.get("/api/v1/documents", headers=session.headers)).status_code == 200


class TestDispatchAfterCommit:
    async def test_the_job_is_published_only_after_it_exists_in_the_database(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        container: Container,
        dispatcher: RecordingDispatcher,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        seen_in_db: list[bool] = []
        original = dispatcher.enqueue_ingestion

        async def checking(job_id: UUID) -> None:
            async with uow_factory() as uow:
                found = (
                    await uow.session.execute(
                        text("SELECT count(*) FROM document_processing_jobs WHERE id = :id"),
                        {"id": job_id},
                    )
                ).scalar_one()
            seen_in_db.append(found == 1)
            await original(job_id)

        dispatcher.enqueue_ingestion = checking  # type: ignore[method-assign]
        session = await make_session()
        assert (await upload(client, session)).status_code == 202
        assert seen_in_db == [True]  # a worker can never receive an id that does not exist

    async def test_a_publish_failure_fails_the_job_with_a_503(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        dispatcher: RecordingDispatcher,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        dispatcher.fail = True
        session = await make_session()
        response = await upload(client, session)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DISPATCH_FAILED"
        async with uow_factory() as uow:
            status, code = (
                await uow.session.execute(
                    text("SELECT status, error_code FROM document_processing_jobs")
                )
            ).one()
        assert (status, code) == ("failed", "DISPATCH_FAILED")

    async def test_retrying_the_same_file_after_a_dispatch_failure_schedules_it(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        dispatcher: RecordingDispatcher,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        session = await make_session()
        data = pdf()
        dispatcher.fail = True
        assert (await upload(client, session, data)).status_code == 503

        dispatcher.fail = False
        retry = await upload(client, session, data)

        assert retry.status_code == 200  # the same document, not a second copy
        body = retry.json()
        assert body["duplicate"] is True
        assert body["document"]["status"] == "queued"
        assert body["job"]["status"] == "queued"
        assert dispatcher.ingestion == [UUID(body["job"]["id"])]
        async with uow_factory() as uow:
            count = (await uow.session.execute(text("SELECT count(*) FROM documents"))).scalar_one()
        assert count == 1

    async def test_a_failed_dispatch_that_fails_again_stays_failed(
        self, client: httpx.AsyncClient, make_session: MakeSession, dispatcher: RecordingDispatcher
    ) -> None:
        session = await make_session()
        data = pdf()
        dispatcher.fail = True
        assert (await upload(client, session, data)).status_code == 503
        assert (await upload(client, session, data)).status_code == 503

    async def test_a_document_that_failed_for_another_reason_is_not_revived(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        container: Container,
        dispatcher: RecordingDispatcher,
    ) -> None:
        session = await make_session()
        data = pdf()
        created = (await upload(client, session, data)).json()
        await container.job_service.mark_failed(
            "document_processing",
            UUID(created["job"]["id"]),
            code="EXTRACTION_FAILED",
            message="The file could not be read.",
        )
        again = await upload(client, session, data)
        assert again.status_code == 200
        assert again.json()["document"]["status"] == "failed"
        assert len(dispatcher.ingestion) == 1  # only the original publish


class TestSweep:
    async def test_a_job_stuck_in_queued_is_republished(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        container: Container,
        dispatcher: RecordingDispatcher,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        session = await make_session()
        job_id = (await upload(client, session)).json()["job"]["id"]
        await age(uow_factory, job_id, minutes=10)  # the original message was lost

        result = await container.sweep_service.run_once()

        assert result.redispatched == 1
        assert dispatcher.ingestion == [UUID(job_id), UUID(job_id)]

    async def test_fresh_and_running_jobs_are_left_alone(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        container: Container,
        dispatcher: RecordingDispatcher,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        session = await make_session()
        fresh = (await upload(client, session)).json()["job"]["id"]
        running = (await upload(client, session)).json()["job"]["id"]
        await age(uow_factory, running, minutes=10)
        await container.job_service.claim("document_processing", UUID(running))

        result = await container.sweep_service.run_once()

        assert result.redispatched == 0
        assert dispatcher.ingestion == [UUID(fresh), UUID(running)]  # only the original publishes

    async def test_a_sweep_does_not_re_publish_the_same_job_every_minute(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        container: Container,
        redis: FakeRedis,
        dispatcher: RecordingDispatcher,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        session = await make_session()
        job_id = (await upload(client, session)).json()["job"]["id"]
        await age(uow_factory, job_id, minutes=10)

        assert (await container.sweep_service.run_once()).redispatched == 1
        redis.values.pop("ciq:test:lock:sweep")  # the next minute's sweep gets the lock
        assert (await container.sweep_service.run_once()).redispatched == 0

        assert len(dispatcher.ingestion) == 2

    async def test_a_republished_job_is_still_run_only_once(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        container: Container,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        """The sweep may race the original message; the claim makes the extra copy harmless."""
        session = await make_session()
        job_id = (await upload(client, session)).json()["job"]["id"]
        await age(uow_factory, job_id, minutes=10)
        await container.sweep_service.run_once()

        first = await container.job_service.claim("document_processing", UUID(job_id))
        second = await container.job_service.claim("document_processing", UUID(job_id))
        assert (first.claimed, second.claimed) == (True, False)
