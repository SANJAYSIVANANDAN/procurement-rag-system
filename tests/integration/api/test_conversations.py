from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

CONVERSATIONS = "/api/v1/conversations"
MakeSession = Callable[[], Any]


async def create(client: httpx.AsyncClient, session: Any, **body: Any) -> dict[str, Any]:
    response = await client.post(CONVERSATIONS, headers=session.headers, json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def error_of(response: httpx.Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


class TestCreate:
    async def test_creates_a_conversation(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        response = await client.post(
            CONVERSATIONS, headers=session.headers, json={"title": "  Liability   review "}
        )
        assert response.status_code == 201
        body = response.json()
        assert set(body) == {
            "id", "title", "vendor_id", "archived", "document_ids", "last_message_seq",
            "created_at", "updated_at",
        }  # fmt: skip
        assert body["title"] == "Liability review"
        assert (body["archived"], body["last_message_seq"], body["document_ids"]) == (False, 0, [])

    async def test_an_empty_body_is_fine(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        assert (
            await client.post(CONVERSATIONS, headers=session.headers, json={})
        ).status_code == 201

    @pytest.mark.parametrize(
        "body",
        [
            {"title": "x" * 201},
            {"unknown": 1},
            {"document_ids": ["not-a-uuid"]},
            {"vendor_id": "x"},
        ],
    )
    async def test_invalid_bodies_are_422(
        self, client: httpx.AsyncClient, make_session: MakeSession, body: dict[str, Any]
    ) -> None:
        session = await make_session()
        response = await client.post(CONVERSATIONS, headers=session.headers, json=body)
        assert response.status_code == 422
        assert error_of(response)["code"] == "VALIDATION_ERROR"

    async def test_requires_authentication(self, client: httpx.AsyncClient) -> None:
        response = await client.post(CONVERSATIONS, json={})
        assert response.status_code == 401
        assert error_of(response)["code"] == "UNAUTHENTICATED"

    async def test_an_unknown_vendor_is_404(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        response = await client.post(
            CONVERSATIONS, headers=session.headers, json={"vendor_id": str(uuid4())}
        )
        assert response.status_code == 404


class TestListAndGet:
    async def test_lists_newest_first_and_pages_with_a_cursor(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        made = [await create(client, session, title=f"c{i}") for i in range(5)]
        seen: list[str] = []
        url = f"{CONVERSATIONS}?limit=2"
        while True:
            body = (await client.get(url, headers=session.headers)).json()
            seen += [c["id"] for c in body["items"]]
            if body["next_cursor"] is None:
                break
            url = f"{CONVERSATIONS}?limit=2&cursor={body['next_cursor']}"
        assert seen == [c["id"] for c in reversed(made)]

    async def test_archived_ones_are_hidden_unless_asked_for(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        keep, old = await create(client, session), await create(client, session)
        await client.patch(
            f"{CONVERSATIONS}/{old['id']}", headers=session.headers, json={"archived": True}
        )
        visible = (await client.get(CONVERSATIONS, headers=session.headers)).json()["items"]
        assert [c["id"] for c in visible] == [keep["id"]]
        everything = (
            await client.get(f"{CONVERSATIONS}?archived=true", headers=session.headers)
        ).json()
        assert {c["id"] for c in everything["items"]} == {keep["id"], old["id"]}

    @pytest.mark.parametrize("query", ["limit=0", "limit=101"])
    async def test_bad_query_parameters_are_422(
        self, client: httpx.AsyncClient, make_session: MakeSession, query: str
    ) -> None:
        session = await make_session()
        response = await client.get(f"{CONVERSATIONS}?{query}", headers=session.headers)
        assert response.status_code == 422

    @pytest.mark.parametrize("cursor", ["garbage", "e30", "W10", "W1wiYVwiLFwiYlwiXQ"])
    async def test_a_tampered_cursor_is_a_400(
        self, client: httpx.AsyncClient, make_session: MakeSession, cursor: str
    ) -> None:
        session = await make_session()
        response = await client.get(f"{CONVERSATIONS}?cursor={cursor}", headers=session.headers)
        assert response.status_code == 400
        assert error_of(response)["code"] == "VALIDATION_ERROR"

    async def test_detail_includes_the_selected_documents(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        created = await create(client, session, title="With docs")
        response = await client.get(f"{CONVERSATIONS}/{created['id']}", headers=session.headers)
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == created["id"] and body["documents"] == []


class TestOwnership:
    """A conversation of another user answers 404 on every endpoint, as if it did not exist."""

    async def test_every_endpoint_hides_someone_elses_conversation(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        alice, bob = await make_session(), await make_session()
        mine = await create(client, alice, title="private")
        url = f"{CONVERSATIONS}/{mine['id']}"

        for response in (
            await client.get(url, headers=bob.headers),
            await client.patch(url, headers=bob.headers, json={"title": "taken"}),
            await client.get(f"{url}/messages", headers=bob.headers),
        ):
            assert response.status_code == 404
            assert error_of(response)["code"] == "NOT_FOUND"
        assert (await client.get(CONVERSATIONS, headers=bob.headers)).json()["items"] == []
        unchanged = (await client.get(url, headers=alice.headers)).json()
        assert unchanged["title"] == "private"

    async def test_an_unknown_id_is_404_and_a_malformed_one_is_422(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        assert (
            await client.get(f"{CONVERSATIONS}/{uuid4()}", headers=session.headers)
        ).status_code == 404
        assert (
            await client.get(f"{CONVERSATIONS}/nope", headers=session.headers)
        ).status_code == 422


class TestUpdate:
    async def test_rename_archive_and_restore(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        created = await create(client, session, title="Old")
        url = f"{CONVERSATIONS}/{created['id']}"
        renamed = await client.patch(url, headers=session.headers, json={"title": "  New   name "})
        assert renamed.status_code == 200 and renamed.json()["title"] == "New name"
        archived = await client.patch(url, headers=session.headers, json={"archived": True})
        assert archived.json()["archived"] is True and archived.json()["title"] == "New name"
        restored = await client.patch(url, headers=session.headers, json={"archived": False})
        assert restored.json()["archived"] is False

    async def test_only_sent_fields_change_and_unknown_ones_are_422(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        created = await create(client, session, title="Keep")
        url = f"{CONVERSATIONS}/{created['id']}"
        assert (await client.patch(url, headers=session.headers, json={})).json()["title"] == "Keep"
        forbidden = await client.patch(url, headers=session.headers, json={"last_message_seq": 9})
        assert forbidden.status_code == 422


class TestMessages:
    async def test_an_empty_conversation_has_no_messages(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        created = await create(client, session)
        response = await client.get(
            f"{CONVERSATIONS}/{created['id']}/messages", headers=session.headers
        )
        assert response.status_code == 200
        assert response.json() == {"items": [], "next_cursor": None}

    @pytest.mark.parametrize("query", ["limit=0", "limit=101", "before_seq=0", "before_seq=x"])
    async def test_bad_query_parameters_are_422(
        self, client: httpx.AsyncClient, make_session: MakeSession, query: str
    ) -> None:
        session = await make_session()
        created = await create(client, session)
        response = await client.get(
            f"{CONVERSATIONS}/{created['id']}/messages?{query}", headers=session.headers
        )
        assert response.status_code == 422
