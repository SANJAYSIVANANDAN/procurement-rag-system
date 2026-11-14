from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from tests.fixtures import contracts as fixtures

VENDORS = "/api/v1/vendors"
MakeSession = Callable[[], Any]


async def create(
    client: httpx.AsyncClient, session: Any, name: str, **extra: Any
) -> dict[str, Any]:
    response = await client.post(VENDORS, headers=session.headers, json={"name": name, **extra})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def error_of(response: httpx.Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


class TestCreate:
    async def test_creates_a_vendor(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        response = await client.post(
            VENDORS,
            headers=session.headers,
            json={
                "name": "  Acme   Corp ",
                "industry": "Software",
                "website": "https://acme.example.com",
                "metadata": {"tier": "strategic"},
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert set(body) == {
            "id",
            "name",
            "industry",
            "website",
            "metadata",
            "created_at",
            "updated_at",
        }
        assert body["name"] == "Acme Corp"
        assert body["metadata"] == {"tier": "strategic"}

    async def test_names_are_unique_per_owner_ignoring_case_and_spacing(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        alice, bob = await make_session(), await make_session()
        await create(client, alice, "Acme Corp")
        for variant in ("acme corp", "ACME   CORP", " Acme Corp "):
            response = await client.post(VENDORS, headers=alice.headers, json={"name": variant})
            assert response.status_code == 409, variant
            assert error_of(response)["code"] == "CONFLICT"
        await create(client, bob, "Acme Corp")  # another user may reuse the name

    @pytest.mark.parametrize(
        "payload",
        [
            {"name": ""},
            {"name": "   "},
            {"name": "x" * 256},
            {},
            {"name": "A", "website": "not a url"},
            {"name": "A", "website": "ftp://acme.example.com"},
            {"name": "A", "unknown_field": 1},
            {"name": "A", "metadata": "string"},
        ],
    )
    async def test_invalid_payloads(
        self, client: httpx.AsyncClient, make_session: MakeSession, payload: dict[str, Any]
    ) -> None:
        session = await make_session()
        response = await client.post(VENDORS, headers=session.headers, json=payload)
        assert response.status_code in (400, 422), response.text
        assert error_of(response)["code"] == "VALIDATION_ERROR"

    async def test_concurrent_creation_of_one_name_yields_one_vendor(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        import asyncio

        session = await make_session()
        responses = await asyncio.gather(
            *(
                client.post(VENDORS, headers=session.headers, json={"name": "Race"})
                for _ in range(6)
            )
        )
        assert sorted(r.status_code for r in responses) == [201, 409, 409, 409, 409, 409]


class TestListAndSearch:
    async def test_list_is_alphabetical_and_scoped_to_the_owner(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        alice, bob = await make_session(), await make_session()
        for name in ("Zeta", "alpha", "Mid"):
            await create(client, alice, name)
        await create(client, bob, "Bobs Vendor")

        response = await client.get(VENDORS, headers=alice.headers)
        assert [v["name"] for v in response.json()["items"]] == ["alpha", "Mid", "Zeta"]

    async def test_cursor_pagination(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        names = [f"Vendor {letter}" for letter in "ABCDE"]
        for name in reversed(names):
            await create(client, session, name)

        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            body = (await client.get(VENDORS, headers=session.headers, params=params)).json()
            seen += [v["name"] for v in body["items"]]
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert seen == names

    async def test_search_matches_substrings(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        for name in ("Acme Cloud Services", "Globex", "Initech"):
            await create(client, session, name)
        response = await client.get(VENDORS, headers=session.headers, params={"q": "CLOUD"})
        assert [v["name"] for v in response.json()["items"]] == ["Acme Cloud Services"]

    async def test_search_tolerates_typos_through_trigram_similarity(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        for name in ("Globex Corporation", "Initech"):
            await create(client, session, name)
        response = await client.get(
            VENDORS, headers=session.headers, params={"q": "globex corporaton"}
        )
        assert [v["name"] for v in response.json()["items"]] == ["Globex Corporation"]

    async def test_search_wildcards_are_literal(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        await create(client, session, "Acme")
        response = await client.get(VENDORS, headers=session.headers, params={"q": "%"})
        assert response.json()["items"] == []


class TestDetailAndUpdate:
    async def test_detail_lists_the_vendors_documents(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        vendor = await create(client, session, "Acme")
        other = await create(client, session, "Other")
        for owner in (vendor, vendor, other):
            await client.post(
                "/api/v1/documents/upload",
                headers=session.headers,
                files={"file": ("a.pdf", fixtures.make_pdf() + uuid4().hex.encode())},
                data={"vendor_id": owner["id"]},
            )
        response = await client.get(f"{VENDORS}/{vendor['id']}", headers=session.headers)
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Acme"
        assert len(body["documents"]) == 2
        assert {d["vendor_id"] for d in body["documents"]} == {vendor["id"]}

    async def test_patch_updates_fields_and_renormalizes_the_name(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        vendor = await create(client, session, "Acme", industry="Software")
        url = f"{VENDORS}/{vendor['id']}"

        renamed = await client.patch(
            url, headers=session.headers, json={"name": "  Acme  Holdings ", "metadata": {"a": 1}}
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Acme Holdings"
        assert renamed.json()["metadata"] == {"a": 1}
        assert renamed.json()["industry"] == "Software"  # untouched

        # the new name is what future duplicates are checked against
        clash = await client.post(VENDORS, headers=session.headers, json={"name": "ACME HOLDINGS"})
        assert clash.status_code == 409
        assert (
            await client.post(VENDORS, headers=session.headers, json={"name": "Acme"})
        ).status_code == 201

    async def test_renaming_to_an_existing_name_conflicts_but_to_itself_is_fine(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        acme = await create(client, session, "Acme")
        await create(client, session, "Globex")
        url = f"{VENDORS}/{acme['id']}"
        clash = await client.patch(url, headers=session.headers, json={"name": "globex"})
        assert clash.status_code == 409
        same = await client.patch(url, headers=session.headers, json={"name": "ACME"})
        assert same.status_code == 200
        assert same.json()["name"] == "ACME"

    async def test_patch_validation(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        vendor = await create(client, session, "Acme")
        url = f"{VENDORS}/{vendor['id']}"
        for payload in ({"name": ""}, {"website": "nope"}, {"owner_id": str(uuid4())}):
            assert (
                await client.patch(url, headers=session.headers, json=payload)
            ).status_code == 422


class TestOwnership:
    async def test_other_users_get_404_everywhere(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        alice, bob = await make_session(), await make_session()
        vendor = await create(client, alice, "Acme")
        url = f"{VENDORS}/{vendor['id']}"

        assert (await client.get(url, headers=bob.headers)).status_code == 404
        patched = await client.patch(url, headers=bob.headers, json={"name": "Stolen"})
        assert patched.status_code == 404
        assert error_of(patched)["code"] == "NOT_FOUND"
        assert (await client.get(url, headers=alice.headers)).json()["name"] == "Acme"
        assert (await client.get(VENDORS, headers=bob.headers)).json()["items"] == []

    async def test_endpoints_require_authentication(self, client: httpx.AsyncClient) -> None:
        for method, path in [
            ("GET", VENDORS),
            ("POST", VENDORS),
            ("GET", f"{VENDORS}/{uuid4()}"),
            ("PATCH", f"{VENDORS}/{uuid4()}"),
        ]:
            response = await client.request(method, path, json={} if method != "GET" else None)
            assert response.status_code == 401, (method, path)
