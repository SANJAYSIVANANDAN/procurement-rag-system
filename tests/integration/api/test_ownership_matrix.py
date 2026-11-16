"""Whether user B can reach anything of user A's, on every endpoint the API has (spec 34.3).

The matrix is checked against the application's own route table: a new endpoint that is neither
listed here nor explicitly exempted fails `test_every_route_is_classified`, so ownership cannot
be forgotten for it. User A really owns a document (with a job), a vendor and a conversation with a
reply; user B must get 404 (never 403, which would confirm the thing exists) for all of them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from covenantiq.api.container import Container
from covenantiq.core.enums import DocumentStatus
from covenantiq.db.scope import OwnershipScope
from covenantiq.services.dto import Principal
from tests.integration.api.conftest import Session
from tests.integration.chat.stack import UowFactory
from tests.integration.factories import make_document

Make = Callable[[], Any]

# Public on purpose: no account needed.
PUBLIC = {
    ("GET", "/api/v1/health/live"),
    ("GET", "/api/v1/health/ready"),
    ("POST", "/api/v1/auth/register"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/token"),
}
# Authenticated, but about the caller only: there is no id of anyone else's to guess.
OWN_ONLY = {
    ("GET", "/api/v1/auth/me"),
    ("POST", "/api/v1/documents/upload"),  # creates; its vendor_id is checked below
    ("GET", "/api/v1/documents"),
    ("POST", "/api/v1/conversations"),  # creates; its document_ids are checked below
    ("GET", "/api/v1/conversations"),
    ("POST", "/api/v1/vendors"),
    ("GET", "/api/v1/vendors"),
}


@dataclass
class Owned:
    document: str
    job: str
    vendor: str
    conversation: str
    reply: str


@pytest.fixture
async def alice(make_session: Make) -> Session:
    session: Session = await make_session()
    return session


@pytest.fixture
async def mallory(make_session: Make) -> Session:
    session: Session = await make_session()
    return session


@pytest.fixture
async def owned(
    client: httpx.AsyncClient, container: Container, uow_factory: UowFactory, alice: Session
) -> Owned:
    scope = OwnershipScope(UUID(alice.id))
    async with uow_factory() as uow:
        document = await uow.documents.add(
            scope, make_document(scope.owner_id, status=DocumentStatus.READY, title="Alice's MSA")
        )
        await uow.commit()
    vendor = (
        await client.post("/api/v1/vendors", json={"name": "Alice Vendor"}, headers=alice.headers)
    ).json()
    conversation = (
        await client.post(
            "/api/v1/conversations",
            json={"title": "Alice chat", "document_ids": [str(document.id)]},
            headers=alice.headers,
        )
    ).json()
    submitted = await container.conversation_service.submit_message(
        Principal(user_id=scope.owner_id, email="a@example.com"),
        UUID(conversation["id"]),
        content="Is liability capped?",
    )
    upload = await client.post(
        "/api/v1/documents/upload",
        headers=alice.headers,
        files={"file": ("a.pdf", b"%PDF-1.7\n" + uuid4().hex.encode())},
    )
    job_id = upload.json().get("job", {}).get("id") if upload.status_code == 202 else None
    return Owned(
        document=str(document.id),
        job=job_id or str(uuid4()),
        vendor=vendor["id"],
        conversation=conversation["id"],
        reply=str(submitted.assistant_message_id),
    )


def matrix(o: Owned) -> list[tuple[str, str, dict[str, Any] | None]]:
    """(method, concrete path, json body) for every endpoint that takes one of A's ids."""
    d, v, c = o.document, o.vendor, o.conversation
    return [
        ("GET", f"/api/v1/documents/{d}", None),
        ("PATCH", f"/api/v1/documents/{d}", {"title": "mine now"}),
        ("DELETE", f"/api/v1/documents/{d}", None),
        ("GET", f"/api/v1/documents/{d}/status", None),
        ("POST", f"/api/v1/documents/{d}/analyze", {}),
        ("GET", f"/api/v1/documents/{d}/findings", None),
        ("GET", f"/api/v1/jobs/{o.job}", None),
        ("GET", f"/api/v1/conversations/{c}", None),
        ("PATCH", f"/api/v1/conversations/{c}", {"title": "mine now"}),
        ("GET", f"/api/v1/conversations/{c}/messages", None),
        ("POST", f"/api/v1/conversations/{c}/messages", {"content": "hello"}),
        ("GET", f"/api/v1/conversations/{c}/stream?message_id={o.reply}", None),
        ("GET", f"/api/v1/vendors/{v}", None),
        ("PATCH", f"/api/v1/vendors/{v}", {"name": "mine now"}),
        ("POST", f"/api/v1/vendors/{v}/assess", None),
        ("GET", f"/api/v1/vendors/{v}/assessments", None),
        ("GET", f"/api/v1/vendors/{v}/assessments/latest", None),
    ]


def template(path: str) -> str:
    """`/api/v1/documents/<uuid>/status` -> `/api/v1/documents/{document_id}/status` shape."""
    return path.split("?")[0]


class TestEveryEndpointOfAnotherUser:
    async def test_user_b_gets_404_on_all_of_user_as_things(
        self, client: httpx.AsyncClient, owned: Owned, mallory: Session
    ) -> None:
        failures = []
        for method, path, body in matrix(owned):
            response = await client.request(method, path, json=body, headers=mallory.headers)
            if response.status_code != 404:
                failures.append(f"{method} {template(path)} -> {response.status_code}")
        assert failures == []

    async def test_the_owner_still_reaches_them_so_the_404s_mean_something(
        self, client: httpx.AsyncClient, owned: Owned, alice: Session
    ) -> None:
        reachable = {
            f"/api/v1/documents/{owned.document}",
            f"/api/v1/documents/{owned.document}/status",
            f"/api/v1/conversations/{owned.conversation}",
            f"/api/v1/conversations/{owned.conversation}/messages",
            f"/api/v1/vendors/{owned.vendor}",
            f"/api/v1/vendors/{owned.vendor}/assessments",
        }
        for path in sorted(reachable):
            response = await client.get(path, headers=alice.headers)
            assert response.status_code == 200, path

    async def test_without_a_token_every_non_public_endpoint_is_401(
        self, client: httpx.AsyncClient, owned: Owned
    ) -> None:
        failures = []
        for method, path, body in matrix(owned):
            response = await client.request(method, path, json=body)
            if response.status_code != 401:
                failures.append(f"{method} {template(path)} -> {response.status_code}")
        for method, path in sorted(OWN_ONLY):
            response = await client.request(method, path)
            if response.status_code != 401:
                failures.append(f"{method} {path} -> {response.status_code}")
        assert failures == []


class TestReferencesToAnotherUsersThings:
    async def test_someone_elses_document_is_not_attached_to_a_new_conversation(
        self, client: httpx.AsyncClient, owned: Owned, mallory: Session
    ) -> None:
        """Ids the caller does not own are dropped without a word (no way to probe for them)."""
        response = await client.post(
            "/api/v1/conversations",
            json={"title": "x", "document_ids": [owned.document]},
            headers=mallory.headers,
        )
        assert response.status_code == 201
        assert response.json()["document_ids"] == []
        detail = await client.get(
            f"/api/v1/conversations/{response.json()['id']}", headers=mallory.headers
        )
        assert detail.json()["documents"] == []  # nothing of A's document is shown through it

    async def test_someone_elses_document_is_not_added_to_a_conversation_scope(
        self, client: httpx.AsyncClient, owned: Owned, mallory: Session
    ) -> None:
        mine = (
            await client.post(
                "/api/v1/conversations", json={"title": "mine"}, headers=mallory.headers
            )
        ).json()
        response = await client.patch(
            f"/api/v1/conversations/{mine['id']}",
            json={"document_ids": [owned.document]},
            headers=mallory.headers,
        )
        assert response.status_code == 200 and response.json()["document_ids"] == []

    async def test_a_document_cannot_be_attached_to_someone_elses_vendor(
        self, client: httpx.AsyncClient, owned: Owned, mallory: Session, uow_factory: UowFactory
    ) -> None:
        scope = OwnershipScope(UUID(mallory.id))
        async with uow_factory() as uow:
            mine = await uow.documents.add(scope, make_document(scope.owner_id))
            await uow.commit()
        response = await client.patch(
            f"/api/v1/documents/{mine.id}",
            json={"vendor_id": owned.vendor},
            headers=mallory.headers,
        )
        assert response.status_code in (404, 422)

    async def test_an_upload_cannot_name_someone_elses_vendor(
        self, client: httpx.AsyncClient, owned: Owned, mallory: Session
    ) -> None:
        response = await client.post(
            "/api/v1/documents/upload",
            headers=mallory.headers,
            data={"vendor_id": owned.vendor},
            files={"file": ("b.pdf", b"%PDF-1.7\n" + uuid4().hex.encode())},
        )
        assert response.status_code in (404, 422)


class TestListsShowOnlyYourOwn:
    async def test_user_bs_lists_do_not_contain_user_as_things(
        self, client: httpx.AsyncClient, owned: Owned, mallory: Session
    ) -> None:
        for path in ("/api/v1/documents", "/api/v1/vendors", "/api/v1/conversations"):
            body = (await client.get(path, headers=mallory.headers)).json()
            assert body["items"] == [], path

    async def test_user_as_lists_do_contain_them(
        self, client: httpx.AsyncClient, owned: Owned, alice: Session
    ) -> None:
        documents = (await client.get("/api/v1/documents", headers=alice.headers)).json()
        assert owned.document in [d["id"] for d in documents["items"]]


class TestEveryRouteIsClassified:
    def test_every_route_is_in_the_matrix_or_explicitly_exempted(
        self, client: httpx.AsyncClient
    ) -> None:
        from covenantiq.api.main import create_app

        spec = create_app().openapi()["paths"]
        routes = {
            (method.upper(), path)
            for path, operations in spec.items()
            for method in operations
            if method in {"get", "post", "put", "patch", "delete"}
        }
        covered = {
            (method, _generic(path))
            for method, path, _ in matrix(Owned("DOC", "JOB", "VEN", "CON", "REP"))
        }
        known = PUBLIC | OWN_ONLY | covered
        unclassified = sorted(routes - known)
        assert unclassified == [], (
            "Add these endpoints to the ownership matrix (or exempt them with a reason): "
            f"{unclassified}"
        )


def _generic(path: str) -> str:
    """`/api/v1/documents/DOC/status` -> `/api/v1/documents/{document_id}/status`."""
    names = {
        "DOC": "{document_id}",
        "JOB": "{job_id}",
        "VEN": "{vendor_id}",
        "CON": "{conversation_id}",
    }
    out = template(path)
    for placeholder, name in names.items():
        out = out.replace(f"/{placeholder}", f"/{name}")
    return out
