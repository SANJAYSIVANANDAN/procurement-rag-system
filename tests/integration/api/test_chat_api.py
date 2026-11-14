"""POST /conversations/{id}/messages through the real application: status codes, the body, errors.
Chat needs a real Redis for its lock, so this module swaps the chat service in the shared
container for one built on CIQ_TEST_REDIS_URL."""

import asyncio
import dataclasses
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from covenantiq.api.container import Container
from covenantiq.db.scope import OwnershipScope
from tests.integration.api.conftest import Session
from tests.integration.chat.stack import Parts, UowFactory, make_parts, retrieval_for, seed_document
from tests.unit.graphs.harness import GOOD_ANSWER, QUESTION

Make = Callable[[], Any]


@pytest_asyncio.fixture
async def parts(
    container: Container, uow_factory: UowFactory, redis_client: Redis, redis_env: str
) -> Parts:
    made, _ = make_parts(
        uow_factory, redis_client, redis_env, retrieval_for(uuid4(), uuid4(), uuid4(), uuid4())
    )
    return made


@pytest.fixture
def container(container: Container, parts: Parts) -> Container:  # type: ignore[no-redef]
    return dataclasses.replace(container, chat_service=parts.service, memory=parts.memory)


@pytest_asyncio.fixture
async def alice(make_session: Make) -> Session:
    session: Session = await make_session()
    return session


async def conversation_with_document(
    client: httpx.AsyncClient, uow_factory: UowFactory, parts: Parts, who: Session
) -> tuple[str, UUID]:
    scope = OwnershipScope(UUID(who.id))
    document_id, clause_id, chunk_id = await seed_document(uow_factory, scope)
    parts.retriever.ready = retrieval_for(document_id, scope.owner_id, clause_id, chunk_id)
    created = await client.post(
        "/api/v1/conversations",
        json={"title": "Chat", "document_ids": [str(document_id)]},
        headers=who.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"], document_id


def url(conversation: str) -> str:
    return f"/api/v1/conversations/{conversation}/messages"


class TestSubmitting:
    async def test_a_question_is_accepted_and_the_reply_appears_in_the_messages(
        self, client: httpx.AsyncClient, uow_factory: UowFactory, parts: Parts, alice: Session
    ) -> None:
        conversation, _ = await conversation_with_document(client, uow_factory, parts, alice)

        response = await client.post(
            url(conversation), json={"content": QUESTION}, headers=alice.headers
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert set(body) == {"user_message_id", "assistant_message_id", "stream_url", "status"}
        assert body["status"] == "pending"
        assert body["stream_url"] == (
            f"/api/v1/conversations/{conversation}/stream?message_id={body['assistant_message_id']}"
        )

        await parts.service.idle()
        listing = await client.get(url(conversation), headers=alice.headers)
        user, reply = listing.json()["items"]
        assert user["id"] == body["user_message_id"] and user["content"] == QUESTION
        assert reply["id"] == body["assistant_message_id"]
        assert reply["status"] == "completed" and reply["content"] == GOOD_ANSWER
        assert reply["grounding_status"] == "grounded"
        assert [c["document_title"] for c in reply["citations"]] == ["Acme MSA"]
        assert reply["citations"][0]["clause_ref"] == "6.2"

    async def test_sending_the_same_client_message_id_again_is_a_200_with_the_same_ids(
        self, client: httpx.AsyncClient, uow_factory: UowFactory, parts: Parts, alice: Session
    ) -> None:
        conversation, _ = await conversation_with_document(client, uow_factory, parts, alice)
        payload = {"content": QUESTION, "client_message_id": str(uuid4())}

        first = await client.post(url(conversation), json=payload, headers=alice.headers)
        await parts.service.idle()
        calls = parts.llm.calls
        again = await client.post(url(conversation), json=payload, headers=alice.headers)

        assert (first.status_code, again.status_code) == (202, 200)
        assert again.json()["assistant_message_id"] == first.json()["assistant_message_id"]
        assert again.json()["status"] == "completed"
        assert parts.llm.calls == calls

    async def test_a_second_question_while_a_reply_is_being_written_is_409(
        self, client: httpx.AsyncClient, uow_factory: UowFactory, parts: Parts, alice: Session
    ) -> None:
        conversation, _ = await conversation_with_document(client, uow_factory, parts, alice)
        parts.retriever.gate = asyncio.Event()
        first = await client.post(
            url(conversation), json={"content": "First?"}, headers=alice.headers
        )
        assert first.status_code == 202
        await asyncio.sleep(0.05)

        second = await client.post(
            url(conversation), json={"content": "Second?"}, headers=alice.headers
        )

        assert second.status_code == 409
        assert second.json()["error"]["code"] == "GENERATION_IN_PROGRESS"
        parts.retriever.gate.set()
        await parts.service.idle()
        listing = await client.get(url(conversation), headers=alice.headers)
        assert len(listing.json()["items"]) == 2  # the refused question was not stored

    async def test_naming_documents_that_are_not_ready_is_a_400(
        self, client: httpx.AsyncClient, uow_factory: UowFactory, parts: Parts, alice: Session
    ) -> None:
        conversation, _ = await conversation_with_document(client, uow_factory, parts, alice)
        response = await client.post(
            url(conversation),
            json={"content": QUESTION, "document_ids": [str(uuid4())]},
            headers=alice.headers,
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


class TestRefusals:
    async def test_it_needs_a_login(self, client: httpx.AsyncClient) -> None:
        response = await client.post(url(str(uuid4())), json={"content": "Hi"})
        assert response.status_code == 401

    async def test_someone_elses_conversation_is_a_404(
        self,
        client: httpx.AsyncClient,
        uow_factory: UowFactory,
        parts: Parts,
        alice: Session,
        make_session: Make,
    ) -> None:
        conversation, _ = await conversation_with_document(client, uow_factory, parts, alice)
        bob: Session = await make_session()
        response = await client.post(url(conversation), json={"content": "Hi"}, headers=bob.headers)
        assert response.status_code == 404
        assert parts.llm.calls == 0

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"content": ""},
            {"content": "x" * 8001},
            {"content": "ok", "client_message_id": "not-a-uuid"},
            {"content": "ok", "surprise": 1},
            {"content": "ok", "document_ids": ["nope"]},
        ],
    )
    async def test_bad_bodies_are_422(
        self,
        client: httpx.AsyncClient,
        uow_factory: UowFactory,
        parts: Parts,
        alice: Session,
        payload: dict[str, Any],
    ) -> None:
        conversation, _ = await conversation_with_document(client, uow_factory, parts, alice)
        response = await client.post(url(conversation), json=payload, headers=alice.headers)
        assert response.status_code == 422

    async def test_with_redis_down_it_is_a_503_and_nothing_is_stored(
        self,
        client: httpx.AsyncClient,
        container: Container,
        uow_factory: UowFactory,
        parts: Parts,
        redis_env: str,
        alice: Session,
    ) -> None:
        conversation, _ = await conversation_with_document(client, uow_factory, parts, alice)
        dead = Redis.from_url(
            "redis://127.0.0.1:1/0", socket_timeout=0.3, socket_connect_timeout=0.3
        )
        broken, _ = make_parts(uow_factory, dead, redis_env, parts.retriever.ready, lock_redis=dead)
        app_container = dataclasses.replace(container, chat_service=broken.service)
        from covenantiq.api.main import create_app

        transport = httpx.ASGITransport(app=create_app(app_container))
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as other:
                response = await other.post(
                    url(conversation), json={"content": "Hi"}, headers=alice.headers
                )
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
            listing = await client.get(url(conversation), headers=alice.headers)
            assert listing.json()["items"] == []
        finally:
            await dead.aclose()
