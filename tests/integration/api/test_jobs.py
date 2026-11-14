from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx

from covenantiq.core.enums import AnalysisJobType
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from tests.fixtures import contracts as fixtures
from tests.integration.factories import make_analysis_job

MakeSession = Callable[[], Any]


async def upload(client: httpx.AsyncClient, session: Any) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/documents/upload",
        headers=session.headers,
        files={"file": ("a.pdf", fixtures.make_pdf() + uuid4().hex.encode())},
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_a_processing_job_is_readable_by_its_owner(
    client: httpx.AsyncClient, make_session: MakeSession
) -> None:
    session = await make_session()
    created = await upload(client, session)
    response = await client.get(f"/api/v1/jobs/{created['job']['id']}", headers=session.headers)
    assert response.status_code == 200
    assert response.json() == {
        "id": created["job"]["id"],
        "kind": "document_processing",
        "status": "queued",
        "stage": None,
        "progress": 0.0,
        "attempt": 0,
        "error": None,
    }


async def test_the_location_header_of_an_upload_points_at_a_working_endpoint(
    client: httpx.AsyncClient, make_session: MakeSession
) -> None:
    session = await make_session()
    response = await client.post(
        "/api/v1/documents/upload",
        headers=session.headers,
        files={"file": ("a.pdf", fixtures.make_pdf())},
    )
    location = response.headers["Location"]
    assert (await client.get(location, headers=session.headers)).status_code == 200


async def test_a_failed_job_exposes_a_safe_error(
    client: httpx.AsyncClient,
    make_session: MakeSession,
    container: Any,
) -> None:
    from uuid import UUID

    session = await make_session()
    created = await upload(client, session)
    await container.job_service.mark_failed(
        "document_processing",
        UUID(created["job"]["id"]),
        code="EXTRACTION_FAILED",
        message="The file could not be read.",
    )
    body = (
        await client.get(f"/api/v1/jobs/{created['job']['id']}", headers=session.headers)
    ).json()
    assert body["status"] == "failed"
    assert body["error"] == {"code": "EXTRACTION_FAILED", "message": "The file could not be read."}


async def test_analysis_jobs_are_served_from_the_same_endpoint(
    client: httpx.AsyncClient,
    make_session: MakeSession,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    session = await make_session()
    created = await upload(client, session)
    from uuid import UUID

    owner = UUID(session.id)
    async with uow_factory() as uow:
        job, _ = await uow.analysis_jobs.create_or_get_active(
            OwnershipScope(owner),
            make_analysis_job(owner, document_id=UUID(created["document"]["id"])),
        )
        await uow.commit()
    response = await client.get(f"/api/v1/jobs/{job.id}", headers=session.headers)
    assert response.status_code == 200
    assert response.json()["kind"] == AnalysisJobType.CONTRACT_ANALYSIS.value


async def test_another_users_job_is_indistinguishable_from_a_missing_one(
    client: httpx.AsyncClient, make_session: MakeSession
) -> None:
    alice, bob = await make_session(), await make_session()
    job_id = (await upload(client, alice))["job"]["id"]

    foreign = await client.get(f"/api/v1/jobs/{job_id}", headers=bob.headers)
    missing = await client.get(f"/api/v1/jobs/{uuid4()}", headers=bob.headers)

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == "NOT_FOUND"
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]


async def test_authentication_is_required_and_ids_are_validated(
    client: httpx.AsyncClient, make_session: MakeSession
) -> None:
    assert (await client.get(f"/api/v1/jobs/{uuid4()}")).status_code == 401
    session = await make_session()
    assert (await client.get("/api/v1/jobs/not-a-uuid", headers=session.headers)).status_code == 422
