"""The vendor assessment endpoints through the real application. The services in the shared
container are swapped for ones that can run a job (real PostgreSQL, real checkpointer, fake
model); the test plays the part of the workers."""

import dataclasses
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from covenantiq.api.container import Container
from covenantiq.db.scope import OwnershipScope
from tests.integration.analysis.stack import clean_checkpoints  # noqa: F401  (a fixture)
from tests.integration.api.conftest import Session
from tests.integration.vendor.stack import UowFactory, VendorStack, build_vendor_stack, seed_vendor

Make = Callable[[], Any]
pytestmark = pytest.mark.usefixtures("clean_checkpoints")


@pytest.fixture
async def stack(container: Container, uow_factory: UowFactory, database_url: str) -> VendorStack:
    return await build_vendor_stack(uow_factory, database_url, documents=1)


@pytest.fixture
def container(container: Container, stack: VendorStack) -> Container:  # type: ignore[no-redef]
    return dataclasses.replace(
        container,
        contract_analysis_service=stack.contract,
        vendor_assessment_service=stack.assessments,
    )


@pytest.fixture
async def alice(make_session: Make) -> Session:
    session: Session = await make_session()
    return session


async def vendor_for(
    who: Session, stack: VendorStack, uow_factory: UowFactory, name: str = "Acme"
) -> UUID:
    vendor_id, _ = await seed_vendor(
        uow_factory, OwnershipScope(UUID(who.id)), name, 1, stack.retriever
    )
    return vendor_id


async def assess(
    client: httpx.AsyncClient, stack: VendorStack, who: Session, vendor_id: UUID
) -> httpx.Response:
    response = await client.post(f"/api/v1/vendors/{vendor_id}/assess", headers=who.headers)
    assert response.status_code == 202, response.text
    await stack.run_pending_contract_analyses()
    result = await stack.assessments.run(UUID(response.json()["job"]["id"]))
    assert result["status"] == "succeeded", result
    return response


class TestAssess:
    async def test_it_needs_a_login(self, client: httpx.AsyncClient) -> None:
        assert (await client.post(f"/api/v1/vendors/{uuid4()}/assess")).status_code == 401

    async def test_it_queues_a_job_and_lists_the_analyses_it_waits_for(
        self, client: httpx.AsyncClient, stack: VendorStack, uow_factory: UowFactory, alice: Session
    ) -> None:
        vendor_id = await vendor_for(alice, stack, uow_factory)
        response = await client.post(f"/api/v1/vendors/{vendor_id}/assess", headers=alice.headers)

        assert response.status_code == 202, response.text
        job = response.json()["job"]
        assert (
            job["kind"] == "vendor_assessment"
            and job["status"] == "queued"
            and job["progress"] == 0
        )
        assert job["scoring_config_version"] == "vendor_scoring_v1@1.0.0"
        assert len(job["pending_dependencies"]) == 1
        assert job["pending_dependencies"][0].startswith("contract_analysis:")
        assert response.headers["location"] == f"/api/v1/jobs/{job['id']}"

    async def test_asking_again_while_it_is_queued_is_a_200_with_the_same_job(
        self, client: httpx.AsyncClient, stack: VendorStack, uow_factory: UowFactory, alice: Session
    ) -> None:
        vendor_id = await vendor_for(alice, stack, uow_factory)
        url = f"/api/v1/vendors/{vendor_id}/assess"
        first = await client.post(url, headers=alice.headers)
        again = await client.post(url, headers=alice.headers)
        assert (first.status_code, again.status_code) == (202, 200)
        assert again.json()["job"]["id"] == first.json()["job"]["id"]

    async def test_a_vendor_with_no_documents_is_a_400(
        self, client: httpx.AsyncClient, alice: Session
    ) -> None:
        created = await client.post(
            "/api/v1/vendors", json={"name": "Empty Ltd"}, headers=alice.headers
        )
        response = await client.post(
            f"/api/v1/vendors/{created.json()['id']}/assess", headers=alice.headers
        )
        assert (
            response.status_code == 400 and response.json()["error"]["code"] == "VALIDATION_ERROR"
        )

    async def test_someone_elses_or_an_unknown_vendor_is_a_404(
        self,
        client: httpx.AsyncClient,
        stack: VendorStack,
        uow_factory: UowFactory,
        alice: Session,
        make_session: Make,
    ) -> None:
        vendor_id = await vendor_for(alice, stack, uow_factory)
        bob: Session = await make_session()
        for target in (vendor_id, uuid4()):
            assert (
                await client.post(f"/api/v1/vendors/{target}/assess", headers=bob.headers)
            ).status_code == 404


class TestReadingAssessments:
    async def test_before_any_assessment_latest_is_a_404_and_history_is_empty(
        self, client: httpx.AsyncClient, stack: VendorStack, uow_factory: UowFactory, alice: Session
    ) -> None:
        vendor_id = await vendor_for(alice, stack, uow_factory)
        assert (
            await client.get(
                f"/api/v1/vendors/{vendor_id}/assessments/latest", headers=alice.headers
            )
        ).status_code == 404
        history = await client.get(
            f"/api/v1/vendors/{vendor_id}/assessments", headers=alice.headers
        )
        assert history.status_code == 200 and history.json() == []

    async def test_a_finished_assessment_is_read_back_whole(
        self, client: httpx.AsyncClient, stack: VendorStack, uow_factory: UowFactory, alice: Session
    ) -> None:
        vendor_id = await vendor_for(alice, stack, uow_factory)
        started = await assess(client, stack, alice, vendor_id)

        response = await client.get(
            f"/api/v1/vendors/{vendor_id}/assessments/latest", headers=alice.headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert (
            body["vendor_id"] == str(vendor_id)
            and body["scoring_config_version"] == "vendor_scoring_v1@1.0.0"
        )
        assert (
            body["overall_score"] == 10
            and body["overall_severity"] == "low"
            and not body["provisional"]
        )
        assert set(body["weights"]) == {
            "legal",
            "data_security",
            "operational",
            "financial_commercial",
            "compliance",
            "dependency",
        }
        assert sum(body["effective_weights"].values()) == pytest.approx(1.0)
        assert body["disclaimer"].startswith("Automated analysis for decision support.")
        assert body["dimensions"][0]["dimension"] == "legal"
        assert len(body["dimensions"]) == 6
        legal = body["dimensions"][0]
        assert legal["status"] == "assessed" and legal["reasoning"] and legal["evidence"]
        assert legal["evidence"][0]["validated"] and legal["evidence"][0]["quote"]
        assert {"signal_id", "label", "weight", "status", "risk", "document_ids"} <= set(
            legal["signals"][0]
        )
        assert body["documents_considered"][0]["title"] == "Acme contract 1"
        job = (
            await client.get(f"/api/v1/jobs/{started.json()['job']['id']}", headers=alice.headers)
        ).json()
        assert job["kind"] == "vendor_assessment" and job["status"] == "succeeded"

    async def test_history_lists_each_assessment_newest_first(
        self, client: httpx.AsyncClient, stack: VendorStack, uow_factory: UowFactory, alice: Session
    ) -> None:
        vendor_id = await vendor_for(alice, stack, uow_factory)
        await assess(client, stack, alice, vendor_id)
        await assess(client, stack, alice, vendor_id)
        response = await client.get(
            f"/api/v1/vendors/{vendor_id}/assessments", params={"limit": 5}, headers=alice.headers
        )
        items = response.json()
        assert len(items) == 2 and items[0]["assessed_at"] >= items[1]["assessed_at"]
        assert all(d["evidence"] == [] for d in items[0]["dimensions"])  # history is the summary
        assert (
            await client.get(
                f"/api/v1/vendors/{vendor_id}/assessments",
                params={"limit": 0},
                headers=alice.headers,
            )
        ).status_code == 422

    async def test_another_user_cannot_read_them(
        self,
        client: httpx.AsyncClient,
        stack: VendorStack,
        uow_factory: UowFactory,
        alice: Session,
        make_session: Make,
    ) -> None:
        vendor_id = await vendor_for(alice, stack, uow_factory)
        await assess(client, stack, alice, vendor_id)
        bob: Session = await make_session()
        for path in ("assessments", "assessments/latest"):
            response = await client.get(f"/api/v1/vendors/{vendor_id}/{path}", headers=bob.headers)
            assert response.status_code == 404
