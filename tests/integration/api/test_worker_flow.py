"""The real Celery tasks, run eagerly inside a worker-style runtime against real PostgreSQL and a
real storage directory. What these cannot show is RabbitMQ delivery itself (see tests/e2e)."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from covenantiq.ai.embeddings.fake import HashEmbeddingProvider
from covenantiq.api.container import Container
from covenantiq.core.config import get_settings
from covenantiq.core.enums import DocumentStatus
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.graphs.checkpointing import psycopg_url
from covenantiq.services.task_dispatcher import NullTaskDispatcher
from tests.fakes import FakeExtractor, healthy_result
from tests.fixtures import contracts as fixtures
from tests.integration.analysis.stack import clean_checkpoints  # noqa: F401  (a fixture)
from tests.integration.factories import make_chunk, make_clause

MakeSession = Callable[[], Any]


def pdf_files(root: Path) -> list[Path]:
    return list(root.rglob("*.pdf"))


def run_task(task_name: str, *args: str, **kwargs: str) -> Any:
    return run_task_with(FakeExtractor(), task_name, *args, **kwargs)


def run_task_with(extractor: Any, task_name: str, *args: str, **kwargs: str) -> Any:
    """Execute a task the way a worker child would: its own event loop and container.

    Blocking, so callers run it with `asyncio.to_thread`.
    """
    from covenantiq.workers import runtime
    from covenantiq.workers.celery_app import celery_app
    from covenantiq.workers.tasks import analysis, ingestion  # noqa: F401

    runtime.install_runtime(
        runtime.build_worker_container(
            get_settings(),
            extractor=extractor,
            embedder=HashEmbeddingProvider(),
            dispatcher=NullTaskDispatcher(),
        ),
        runtime.new_event_loop(),
    )
    try:
        return celery_app.tasks[task_name].apply(args=list(args), kwargs=kwargs)
    finally:
        runtime.shutdown_runtime()


async def run(task_name: str, *args: str, **kwargs: str) -> Any:
    return await asyncio.to_thread(run_task, task_name, *args, **kwargs)


async def run_with(extractor: Any, task_name: str, *args: str, **kwargs: str) -> Any:
    return await asyncio.to_thread(run_task_with, extractor, task_name, *args, **kwargs)


async def upload(client: httpx.AsyncClient, session: Any) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/documents/upload",
        headers=session.headers,
        files={"file": ("a.pdf", fixtures.make_pdf(2) + uuid4().hex.encode())},
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def job_of(client: httpx.AsyncClient, session: Any, job_id: str) -> dict[str, Any]:
    body: dict[str, Any] = (
        await client.get(f"/api/v1/jobs/{job_id}", headers=session.headers)
    ).json()
    return body


async def document_of(client: httpx.AsyncClient, session: Any, document_id: str) -> dict[str, Any]:
    body: dict[str, Any] = (
        await client.get(f"/api/v1/documents/{document_id}", headers=session.headers)
    ).json()
    return body


async def test_uploading_then_processing_moves_the_job_through_both_stages(
    client: httpx.AsyncClient, make_session: MakeSession
) -> None:
    session = await make_session()
    created = await upload(client, session)
    job_id, document_id = created["job"]["id"], created["document"]["id"]
    assert (await job_of(client, session, job_id))["status"] == "queued"

    result = await run("ingest.process_document", job_id, request_id="req-flow")

    assert result.state == "SUCCESS"
    assert result.result["status"] == "chunked"
    assert result.result["chunks"] >= 1
    job = await job_of(client, session, job_id)
    assert (job["status"], job["stage"], job["attempt"]) == ("queued", "embedding", 1)
    document = await document_of(client, session, document_id)
    assert document["status"] == "embedding"
    assert document["extraction_quality"]["text_chars"] > 0

    embedded = await run("ingest.embed_document", job_id)

    assert embedded.state == "SUCCESS"
    assert embedded.result["status"] == "ready"
    job = await job_of(client, session, job_id)
    assert (job["status"], job["progress"], job["attempt"]) == ("succeeded", 1.0, 2)
    document = await document_of(client, session, document_id)
    assert document["status"] == "ready"
    assert document["chunk_count"] == embedded.result["chunks"]


async def test_a_duplicate_delivery_is_ignored(
    client: httpx.AsyncClient, make_session: MakeSession
) -> None:
    session = await make_session()
    job_id = (await upload(client, session))["job"]["id"]

    await run("ingest.process_document", job_id)
    await run("ingest.embed_document", job_id)
    again = await run("ingest.process_document", job_id)  # the broker delivers it twice

    assert again.state == "SUCCESS"
    assert again.result == {"status": "skipped"}
    assert (await job_of(client, session, job_id))["attempt"] == 2  # one run per stage


async def test_a_message_for_a_job_that_does_not_exist_is_rejected_not_crashed(
    client: httpx.AsyncClient,
) -> None:
    result = await run("ingest.process_document", str(uuid4()))
    assert result.state == "REJECTED"  # dead-lettered, and the worker carries on


async def test_a_worker_that_died_mid_task_is_replaced_by_the_redelivered_message(
    client: httpx.AsyncClient,
    make_session: MakeSession,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    session = await make_session()
    created = await upload(client, session)
    job_id = created["job"]["id"]

    # worker A claimed the job and then died: running, heartbeat 11 minutes old
    async with uow_factory() as uow:
        await uow.session.execute(
            text(
                "UPDATE document_processing_jobs SET status = 'running', attempt = 1, "
                "heartbeat_at = now() - interval '11 minutes' WHERE id = :id"
            ),
            {"id": UUID(job_id)},
        )
        await uow.commit()

    result = await run("ingest.process_document", job_id)  # RabbitMQ redelivers it to worker B

    assert result.result["status"] == "chunked"
    job = await job_of(client, session, job_id)
    assert (job["status"], job["attempt"]) == ("queued", 2)  # taken over, then handed on


async def test_a_message_for_a_job_a_live_worker_owns_is_left_alone(
    client: httpx.AsyncClient,
    make_session: MakeSession,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    session = await make_session()
    job_id = (await upload(client, session))["job"]["id"]
    async with uow_factory() as uow:
        await uow.session.execute(
            text(
                "UPDATE document_processing_jobs SET status = 'running', attempt = 1, "
                "heartbeat_at = now() WHERE id = :id"
            ),
            {"id": UUID(job_id)},
        )
        await uow.commit()

    result = await run("ingest.process_document", job_id)

    assert result.result == {"status": "skipped"}
    assert (await job_of(client, session, job_id))["status"] == "running"


async def test_a_task_that_cannot_succeed_fails_the_job_then_replay_recovers_it(
    client: httpx.AsyncClient, make_session: MakeSession, container: Container
) -> None:
    session = await make_session()
    created = await upload(client, session)
    job_id, document_id = created["job"]["id"], created["document"]["id"]

    # a scan with no text is a permanent failure, so Postgres must show it before the message
    # would be dead-lettered
    empty = FakeExtractor(healthy_result(pages=3, items=[]))
    result = await run_with(empty, "ingest.process_document", job_id)
    assert result.state == "REJECTED"
    job = await job_of(client, session, job_id)
    assert job["status"] == "failed"
    assert job["error"]["code"] == "NO_EXTRACTABLE_TEXT"
    assert (await document_of(client, session, document_id))["status"] == "failed"

    # `covenantiq dlq replay` resets the job (compare-and-set from failed) and republishes
    assert await container.job_service.reset_for_replay("document_processing", UUID(job_id))
    assert (await job_of(client, session, job_id))["status"] == "queued"
    assert (await document_of(client, session, document_id))["status"] == "queued"

    recovered = await run("ingest.process_document", job_id)
    assert recovered.result["status"] == "chunked"
    assert (await job_of(client, session, job_id))["status"] == "queued"  # waiting to be embedded


@pytest.fixture
def checkpoint_database(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the worker's LangGraph checkpointer at the test database (tables created for it)."""
    monkeypatch.setenv("CIQ_CHECKPOINT_DB_URL", psycopg_url(database_url))
    get_settings.cache_clear()


async def analysis_job_for(
    client: httpx.AsyncClient,
    session: Any,
    uow_factory: Callable[[], UnitOfWork],
    *,
    ready: bool,
) -> tuple[UUID, UUID]:
    from tests.integration.factories import make_analysis_job

    created = await upload(client, session)
    owner, document_id = UUID(session.id), UUID(created["document"]["id"])
    async with uow_factory() as uow:
        if ready:
            await uow.documents.system_set_status(
                document_id, [DocumentStatus.QUEUED], DocumentStatus.READY
            )
        job, _ = await uow.analysis_jobs.create_or_get_active(
            OwnershipScope(owner), make_analysis_job(owner, document_id=document_id)
        )
        await uow.commit()
    return job.id, owner


@pytest.mark.usefixtures("clean_checkpoints", "checkpoint_database")
async def test_the_analysis_task_fails_a_job_on_a_document_that_is_not_ready(
    client: httpx.AsyncClient,
    make_session: MakeSession,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    session = await make_session()
    job_id, _ = await analysis_job_for(client, session, uow_factory, ready=False)

    result = await run("analysis.run_contract_analysis", str(job_id))

    assert result.state == "REJECTED"
    body = await job_of(client, session, str(job_id))
    assert (body["status"], body["error"]["code"]) == ("failed", "DOCUMENT_NOT_READY")


@pytest.mark.usefixtures("clean_checkpoints", "checkpoint_database")
async def test_the_analysis_task_runs_the_real_graph_and_fails_cleanly_without_a_usable_model(
    client: httpx.AsyncClient,
    make_session: MakeSession,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    """The worker container is built from settings (fake model, real retrieval engine, real
    checkpointer). The fake model's default answer is not valid output, so every batch degrades
    and the job ends `failed` with ANALYSIS_DEGRADED, and no finding is stored."""
    session = await make_session()
    job_id, _ = await analysis_job_for(client, session, uow_factory, ready=True)

    result = await run("analysis.run_contract_analysis", str(job_id))

    assert result.state == "REJECTED"
    body = await job_of(client, session, str(job_id))
    assert (body["status"], body["error"]["code"]) == ("failed", "ANALYSIS_DEGRADED")
    async with uow_factory() as uow:
        stored = (
            await uow.session.execute(text("SELECT count(*) FROM contract_findings"))
        ).scalar_one()
    assert stored == 0


async def vendor_job_for(
    client: httpx.AsyncClient,
    session: Any,
    uow_factory: Callable[[], UnitOfWork],
    *,
    depends_on: list[UUID] | None = None,
    job_type: Any = None,
) -> tuple[UUID, UUID]:
    """A ready document linked to a new vendor, and a queued vendor job for it."""
    from covenantiq.core.enums import AnalysisJobType
    from tests.integration.factories import make_analysis_job, make_vendor

    created = await upload(client, session)
    owner, document_id = UUID(session.id), UUID(created["document"]["id"])
    async with uow_factory() as uow:
        scope = OwnershipScope(owner)
        vendor = await uow.vendors.add(scope, make_vendor(owner))
        await uow.session.execute(
            text("UPDATE documents SET vendor_id = :v, status = 'ready' WHERE id = :d"),
            {"v": vendor.id, "d": document_id},
        )
        job = make_analysis_job(
            owner, vendor_id=vendor.id, job_type=job_type or AnalysisJobType.VENDOR_ASSESSMENT
        )
        job.params = {"depends_on": [str(d) for d in depends_on or []]}
        stored, _ = await uow.analysis_jobs.create_or_get_active(scope, job)
        await uow.commit()
    return stored.id, document_id


@pytest.mark.usefixtures("clean_checkpoints", "checkpoint_database")
async def test_a_vendor_assessment_gives_up_after_a_bounded_wait_for_its_dependency(
    client: httpx.AsyncClient,
    make_session: MakeSession,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    from tests.integration.factories import make_analysis_job

    session = await make_session()
    created = await upload(client, session)
    owner = UUID(session.id)
    async with uow_factory() as uow:  # a contract analysis that has not run yet
        dependency, _ = await uow.analysis_jobs.create_or_get_active(
            OwnershipScope(owner),
            make_analysis_job(owner, document_id=UUID(created["document"]["id"])),
        )
        await uow.commit()
    job_id, _ = await vendor_job_for(client, session, uow_factory, depends_on=[dependency.id])

    result = await run("analysis.run_vendor_assessment", str(job_id))

    # Celery's eager mode re-runs a retried task inline, so this plays out the whole bounded wait:
    # twenty "still running, ask again later" answers, then the job fails with a clear code.
    assert result.state == "REJECTED"
    body = await job_of(client, session, str(job_id))
    assert (body["status"], body["error"]["code"]) == ("failed", "DEPENDENCY_NOT_READY")


@pytest.mark.usefixtures("clean_checkpoints", "checkpoint_database")
async def test_a_vendor_assessment_whose_documents_were_never_analysed_fails_clearly(
    client: httpx.AsyncClient,
    make_session: MakeSession,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    session = await make_session()
    job_id, _ = await vendor_job_for(client, session, uow_factory)

    result = await run("analysis.run_vendor_assessment", str(job_id))

    assert result.state == "REJECTED"
    body = await job_of(client, session, str(job_id))
    assert (body["status"], body["error"]["code"]) == ("failed", "DEPENDENCY_NOT_READY")


@pytest.mark.usefixtures("clean_checkpoints", "checkpoint_database")
async def test_a_comparison_task_for_vendors_without_assessments_fails_with_the_reason(
    client: httpx.AsyncClient,
    make_session: MakeSession,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    from covenantiq.core.enums import AnalysisJobType
    from covenantiq.db.models import VendorComparison

    session = await make_session()
    job_id, _ = await vendor_job_for(
        client, session, uow_factory, job_type=AnalysisJobType.VENDOR_COMPARISON
    )
    owner = UUID(session.id)
    async with uow_factory() as uow:
        await uow.comparisons.add(
            OwnershipScope(owner),
            VendorComparison(
                owner_id=owner,
                analysis_job_id=job_id,
                vendor_ids=[str(uuid4()), str(uuid4())],
                assessment_ids=[],
                result={},
            ),
        )
        await uow.commit()

    result = await run("analysis.run_vendor_comparison", str(job_id))

    assert result.state == "REJECTED"
    body = await job_of(client, session, str(job_id))
    assert (body["status"], body["error"]["code"]) == ("failed", "ASSESSMENT_REQUIRED")


async def test_purge_removes_a_deleted_documents_rows_and_file(
    client: httpx.AsyncClient,
    make_session: MakeSession,
    storage_root: Path,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    session = await make_session()
    created = await upload(client, session)
    document_id = UUID(created["document"]["id"])
    owner = UUID(session.id)
    scope = OwnershipScope(owner)
    async with uow_factory() as uow:
        clause = make_clause(owner, document_id, 0)
        await uow.clauses.replace_for_document(scope, document_id, [clause])
        await uow.chunks.bulk_insert(
            scope, [make_chunk(owner, document_id, clause.id, n) for n in range(3)]
        )
        await uow.commit()
    assert pdf_files(storage_root)

    await client.delete(f"/api/v1/documents/{document_id}", headers=session.headers)
    result = await run("ingest.purge_document", str(document_id))

    assert result.result == {"status": "purged", "chunks": 3}
    assert pdf_files(storage_root) == []
    async with uow_factory() as uow:
        for table in ("document_chunks", "document_clauses"):
            count = (
                await uow.session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE document_id = :d"),  # noqa: S608
                    {"d": document_id},
                )
            ).scalar_one()
            assert count == 0


async def test_purge_never_touches_a_live_document(
    client: httpx.AsyncClient, make_session: MakeSession, storage_root: Path
) -> None:
    session = await make_session()
    created = await upload(client, session)

    result = await run("ingest.purge_document", created["document"]["id"])

    assert result.result == {"status": "skipped"}
    assert len(pdf_files(storage_root)) == 1


async def test_purge_of_an_unknown_document_is_a_quiet_no_op(client: httpx.AsyncClient) -> None:
    result = await run("ingest.purge_document", str(uuid4()))
    assert result.result == {"status": "skipped"}
