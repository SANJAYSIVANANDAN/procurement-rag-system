"""POST /documents/{id}/analyze and GET /documents/{id}/findings through the real application.
The analysis service in the shared container is swapped for one that can run a job (real
PostgreSQL, real checkpointer, fake model)."""

import dataclasses
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from covenantiq.api.container import Container
from covenantiq.core.enums import DocumentStatus
from covenantiq.db.scope import OwnershipScope
from tests.integration.analysis.stack import Stack, build_stack, clean_checkpoints  # noqa: F401
from tests.integration.api.conftest import Session
from tests.integration.chat.stack import UowFactory, retrieval_for, seed_document

Make = Callable[[], Any]
pytestmark = pytest.mark.usefixtures("clean_checkpoints")


@pytest.fixture
async def stack(container: Container, uow_factory: UowFactory, database_url: str) -> Stack:
    return await build_stack(uow_factory, database_url)


@pytest.fixture
def container(container: Container, stack: Stack) -> Container:  # type: ignore[no-redef]
    return dataclasses.replace(container, contract_analysis_service=stack.service)


async def document_for(
    who: Session, stack: Stack, uow_factory: UowFactory, *, ready: bool = True
) -> UUID:
    scope = OwnershipScope(UUID(who.id))
    document_id, clause_id, chunk_id = await seed_document(uow_factory, scope)
    stack.retriever.ready = retrieval_for(document_id, scope.owner_id, clause_id, chunk_id)
    if not ready:
        async with uow_factory() as uow:
            await uow.documents.system_set_status(
                document_id, [DocumentStatus.READY], DocumentStatus.EMBEDDING
            )
            await uow.commit()
    return document_id


@pytest.fixture
async def alice(make_session: Make) -> Session:
    session: Session = await make_session()
    return session


class TestAnalyze:
    async def test_it_needs_a_login(self, client: httpx.AsyncClient) -> None:
        assert (await client.post(f"/api/v1/documents/{uuid4()}/analyze")).status_code == 401

    async def test_it_queues_a_job_and_says_which_ruleset(
        self, client: httpx.AsyncClient, stack: Stack, uow_factory: UowFactory, alice: Session
    ) -> None:
        document_id = await document_for(alice, stack, uow_factory)
        response = await client.post(
            f"/api/v1/documents/{document_id}/analyze", headers=alice.headers
        )

        assert response.status_code == 202, response.text
        job = response.json()["job"]
        assert job["kind"] == "contract_analysis" and job["status"] == "queued"
        assert job["progress"] == 0 and job["ruleset_version"] == "contract_baseline_v1@1.0.0"
        assert response.headers["location"] == f"/api/v1/jobs/{job['id']}"
        assert [str(j) for _, j in stack.dispatcher.analysis] == [job["id"]]

    async def test_asking_again_while_it_is_queued_is_a_200_with_the_same_job(
        self, client: httpx.AsyncClient, stack: Stack, uow_factory: UowFactory, alice: Session
    ) -> None:
        document_id = await document_for(alice, stack, uow_factory)
        url = f"/api/v1/documents/{document_id}/analyze"
        first = await client.post(url, headers=alice.headers)
        again = await client.post(url, json={"force": True}, headers=alice.headers)
        assert (first.status_code, again.status_code) == (202, 200)
        assert again.json()["job"]["id"] == first.json()["job"]["id"]
        assert len(stack.dispatcher.analysis) == 1

    async def test_a_document_still_processing_is_a_409(
        self, client: httpx.AsyncClient, stack: Stack, uow_factory: UowFactory, alice: Session
    ) -> None:
        document_id = await document_for(alice, stack, uow_factory, ready=False)
        response = await client.post(
            f"/api/v1/documents/{document_id}/analyze", headers=alice.headers
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DOCUMENT_NOT_READY"

    async def test_someone_elses_or_an_unknown_document_is_a_404(
        self,
        client: httpx.AsyncClient,
        stack: Stack,
        uow_factory: UowFactory,
        alice: Session,
        make_session: Make,
    ) -> None:
        document_id = await document_for(alice, stack, uow_factory)
        bob: Session = await make_session()
        for target in (document_id, uuid4()):
            response = await client.post(f"/api/v1/documents/{target}/analyze", headers=bob.headers)
            assert response.status_code == 404

    @pytest.mark.parametrize(
        ("payload", "status"),
        [({"ruleset": "made_up"}, 400), ({"surprise": 1}, 422), ({"force": "maybe"}, 422)],
    )
    async def test_bad_requests(
        self,
        client: httpx.AsyncClient,
        stack: Stack,
        uow_factory: UowFactory,
        alice: Session,
        payload: dict[str, Any],
        status: int,
    ) -> None:
        document_id = await document_for(alice, stack, uow_factory)
        response = await client.post(
            f"/api/v1/documents/{document_id}/analyze", json=payload, headers=alice.headers
        )
        assert response.status_code == status


class TestFindings:
    async def run(
        self, client: httpx.AsyncClient, stack: Stack, alice: Session, document_id: UUID
    ) -> str:
        response = await client.post(
            f"/api/v1/documents/{document_id}/analyze", headers=alice.headers
        )
        job_id = response.json()["job"]["id"]
        await stack.service.run(UUID(job_id))
        return str(job_id)

    async def test_before_any_analysis_it_is_a_404(
        self, client: httpx.AsyncClient, stack: Stack, uow_factory: UowFactory, alice: Session
    ) -> None:
        document_id = await document_for(alice, stack, uow_factory)
        response = await client.get(
            f"/api/v1/documents/{document_id}/findings", headers=alice.headers
        )
        assert response.status_code == 404

    async def test_a_finished_analysis_is_read_back_whole(
        self, client: httpx.AsyncClient, stack: Stack, uow_factory: UowFactory, alice: Session
    ) -> None:
        document_id = await document_for(alice, stack, uow_factory)
        stack.retriever.set_passage("The Supplier accepts unlimited liability for any loss.")
        job_id = await self.run(client, stack, alice, document_id)

        response = await client.get(
            f"/api/v1/documents/{document_id}/findings", headers=alice.headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["job_id"] == job_id and body["document_id"] == str(document_id)
        assert body["ruleset_version"] == "contract_baseline_v1@1.0.0"
        assert body["disclaimer"].startswith("Automated analysis for decision support.")
        assert body["summary"]["critical"] >= 1 and "absent" in body["summary"]
        first = body["findings"][0]
        assert first["severity"] == "critical" and first["clause_status"] == "problematic"
        assert first["rule_id"] == "LIAB-001" and first["risk_category"] == "liability"
        assert first["evidence"] and first["evidence"][0]["validated"] is True
        assert first["evidence"][0]["document_title"] == "Acme MSA"
        assert set(first) >= {
            "id",
            "title",
            "explanation",
            "business_impact",
            "recommended_action",
            "clause_ref",
            "page_number",
            "confidence",
            "evidence_sufficiency",
            "analyzed_at",
        }

    async def test_filters_and_a_specific_job(
        self, client: httpx.AsyncClient, stack: Stack, uow_factory: UowFactory, alice: Session
    ) -> None:
        document_id = await document_for(alice, stack, uow_factory)
        stack.retriever.set_passage("The Supplier accepts unlimited liability for any loss.")
        job_id = await self.run(client, stack, alice, document_id)
        url = f"/api/v1/documents/{document_id}/findings"

        critical = await client.get(url, params={"severity": "critical"}, headers=alice.headers)
        assert {f["severity"] for f in critical.json()["findings"]} == {"critical"}
        liability = await client.get(
            url, params={"category": "liability", "status": "problematic"}, headers=alice.headers
        )
        assert liability.json()["findings"] and all(
            f["risk_category"] == "liability" for f in liability.json()["findings"]
        )
        named = await client.get(url, params={"job_id": job_id}, headers=alice.headers)
        assert named.json()["job_id"] == job_id
        for bad in ({"severity": "huge"}, {"status": "nope"}, {"job_id": "x"}):
            assert (await client.get(url, params=bad, headers=alice.headers)).status_code == 422
        unknown_job = await client.get(url, params={"job_id": str(uuid4())}, headers=alice.headers)
        assert unknown_job.status_code == 404

    async def test_another_user_cannot_read_them(
        self,
        client: httpx.AsyncClient,
        stack: Stack,
        uow_factory: UowFactory,
        alice: Session,
        make_session: Make,
    ) -> None:
        document_id = await document_for(alice, stack, uow_factory)
        await self.run(client, stack, alice, document_id)
        bob: Session = await make_session()
        response = await client.get(
            f"/api/v1/documents/{document_id}/findings", headers=bob.headers
        )
        assert response.status_code == 404

    async def test_the_job_endpoint_shows_the_analysis_succeeded(
        self, client: httpx.AsyncClient, stack: Stack, uow_factory: UowFactory, alice: Session
    ) -> None:
        document_id = await document_for(alice, stack, uow_factory)
        job_id = await self.run(client, stack, alice, document_id)
        job = (await client.get(f"/api/v1/jobs/{job_id}", headers=alice.headers)).json()
        assert job["kind"] == "contract_analysis" and job["status"] == "succeeded"
        assert job["progress"] == 1
