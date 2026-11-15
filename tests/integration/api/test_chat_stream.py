"""GET /conversations/{id}/stream through the real application, on real PostgreSQL and Redis.

The chat service writes to a real Redis Stream through `RedisStreamPublisher`; the listener reads it
back through `StreamConsumer`, `StreamService` and the SSE endpoint. The model is a scripted fake
and the retriever returns a prepared result.
"""

import asyncio
import dataclasses
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from httpx_sse import aconnect_sse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from covenantiq.api.container import Container
from covenantiq.core import redis_keys
from covenantiq.core.enums import MessageStatus
from covenantiq.db.models import Message
from covenantiq.db.scope import OwnershipScope
from covenantiq.services.dto import Principal
from covenantiq.services.stream_service import StreamService
from covenantiq.streaming.consumer import StreamConsumer
from covenantiq.streaming.events import check_order, parse_event
from covenantiq.streaming.publisher import RedisStreamPublisher
from tests.integration.api.conftest import Session
from tests.integration.chat.stack import Parts, UowFactory, make_parts, retrieval_for, seed_document
from tests.unit.graphs.harness import GOOD_ANSWER, QUESTION

Make = Callable[[], Any]


@pytest.fixture
def publisher(redis_client: Redis, redis_env: str) -> RedisStreamPublisher:
    return RedisStreamPublisher(redis_client, env=redis_env, flush_interval=0.01)


@pytest_asyncio.fixture
async def parts(
    container: Container,
    uow_factory: UowFactory,
    redis_client: Redis,
    redis_env: str,
    publisher: RedisStreamPublisher,
) -> Parts:
    made, _ = make_parts(
        uow_factory,
        redis_client,
        redis_env,
        retrieval_for(uuid4(), uuid4(), uuid4(), uuid4()),
        publisher=publisher,
        stale_after=timedelta(0),
    )
    return made


@pytest.fixture
def consumer(redis_client: Redis, redis_env: str) -> StreamConsumer:
    return StreamConsumer(redis_client, env=redis_env, idle_timeout=0.4, block_ms=100)


@pytest.fixture
def container(  # type: ignore[no-redef]
    container: Container, parts: Parts, uow_factory: UowFactory, consumer: StreamConsumer
) -> Container:
    return dataclasses.replace(
        container,
        chat_service=parts.service,
        memory=parts.memory,
        stream_service=StreamService(consumer, uow_factory),
    )


@pytest_asyncio.fixture
async def alice(make_session: Make) -> Session:
    session: Session = await make_session()
    return session


@dataclasses.dataclass
class Reply:
    conversation: str
    message: str
    session: Session

    @property
    def url(self) -> str:
        return f"/api/v1/conversations/{self.conversation}/stream?message_id={self.message}"


async def ask(
    client: httpx.AsyncClient,
    uow_factory: UowFactory,
    parts: Parts,
    who: Session,
    *,
    wait: bool = True,
) -> Reply:
    scope = OwnershipScope(UUID(who.id))
    document_id, clause_id, chunk_id = await seed_document(uow_factory, scope)
    parts.retriever.ready = retrieval_for(document_id, scope.owner_id, clause_id, chunk_id)
    created = await client.post(
        "/api/v1/conversations",
        json={"title": "Chat", "document_ids": [str(document_id)]},
        headers=who.headers,
    )
    conversation = created.json()["id"]
    sent = await client.post(
        f"/api/v1/conversations/{conversation}/messages",
        json={"content": QUESTION},
        headers=who.headers,
    )
    assert sent.status_code == 202, sent.text
    if wait:
        await parts.service.idle()
    return Reply(conversation, sent.json()["assistant_message_id"], who)


class Got:
    def __init__(self) -> None:
        self.frames: list[tuple[str, str, dict[str, Any]]] = []  # (id, event, data)

    @property
    def ids(self) -> list[str]:
        return [f[0] for f in self.frames]

    @property
    def kinds(self) -> list[str]:
        return [f[1] for f in self.frames]

    def last(self) -> dict[str, Any]:
        return self.frames[-1][2]

    def check(self) -> None:
        check_order(parse_event(f[2]) for f in self.frames)


async def listen(
    client: httpx.AsyncClient, reply: Reply, *, last_event_id: str | None = None
) -> Got:
    headers = dict(reply.session.headers)
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    got = Got()
    async with aconnect_sse(client, "GET", reply.url, headers=headers) as source:
        assert source.response.status_code == 200, await source.response.aread()
        assert source.response.headers["content-type"].startswith("text/event-stream")
        assert source.response.headers["x-accel-buffering"] == "no"
        assert "no-cache" in source.response.headers["cache-control"]
        async for sse in source.aiter_sse():
            got.frames.append((sse.id, sse.event, json.loads(sse.data)))
    return got


class TestLiveStream:
    async def test_events_arrive_in_order_and_end_with_the_validated_answer(
        self, client: httpx.AsyncClient, uow_factory: UowFactory, parts: Parts, alice: Session
    ) -> None:
        reply = await ask(client, uow_factory, parts, alice)

        got = await listen(client, reply)

        got.check()
        assert got.kinds[0] == "message_start" and got.kinds[-1] == "message_complete"
        assert "token" in got.kinds and "citation" in got.kinds
        assert got.last()["content"] == GOOD_ANSWER
        assert got.last()["status"] == "completed" and got.last()["grounding_status"] == "grounded"
        tokens = "".join(d["text"] for _, k, d in got.frames if k == "token")
        assert tokens.strip()  # the draft text streamed before the validated answer
        assert all("-" in i and int(i.split("-")[0]) > 0 for i in got.ids)  # redis entry ids
        citation = next(d for _, k, d in got.frames if k == "citation")["citation"]
        assert citation["document_title"] == "Acme MSA" and citation["clause_ref"] == "6.2"

    async def test_listening_before_the_reply_is_finished_gets_the_whole_stream(
        self, client: httpx.AsyncClient, uow_factory: UowFactory, parts: Parts, alice: Session
    ) -> None:
        parts.retriever.gate = asyncio.Event()
        reply = await ask(client, uow_factory, parts, alice, wait=False)
        listener = asyncio.create_task(listen(client, reply))
        await asyncio.sleep(0.25)  # the listener is waiting with the run held at retrieval
        assert not listener.done()

        parts.retriever.gate.set()
        got = await asyncio.wait_for(listener, 20)

        got.check()
        assert got.kinds[-1] == "message_complete"

    async def test_the_reply_completes_in_the_database_when_the_listener_leaves(
        self,
        consumer: StreamConsumer,
        client: httpx.AsyncClient,
        uow_factory: UowFactory,
        parts: Parts,
        alice: Session,
    ) -> None:
        parts.retriever.gate = asyncio.Event()
        reply = await ask(client, uow_factory, parts, alice, wait=False)
        service = StreamService(consumer, uow_factory)
        gone = False

        async def disconnected() -> bool:
            return gone

        frames = await service.open(
            Principal(user_id=UUID(alice.id), email="a@example.com"),
            UUID(reply.conversation),
            UUID(reply.message),
            disconnected=disconnected,
        )
        first = await frames.__anext__()  # message_start
        assert first.event == "message_start"
        gone = True
        assert [f async for f in frames] == []  # the listener's loop ends at once

        parts.retriever.gate.set()
        await parts.service.idle()
        async with uow_factory() as uow:
            row = (
                await uow.session.execute(select(Message).where(Message.id == UUID(reply.message)))
            ).scalar_one()
        assert row.status is MessageStatus.COMPLETED and row.content == GOOD_ANSWER


class TestResuming:
    async def test_reconnecting_with_the_last_id_gives_exactly_the_rest(
        self, client: httpx.AsyncClient, uow_factory: UowFactory, parts: Parts, alice: Session
    ) -> None:
        reply = await ask(client, uow_factory, parts, alice)
        everything = await listen(client, reply)
        assert len(everything.frames) >= 4

        for cut in range(len(everything.frames)):
            rest = await listen(client, reply, last_event_id=everything.ids[cut])
            assert rest.frames == everything.frames[cut + 1 :], f"after frame {cut}"

    async def test_reconnecting_without_an_id_starts_from_message_start(
        self, client: httpx.AsyncClient, uow_factory: UowFactory, parts: Parts, alice: Session
    ) -> None:
        reply = await ask(client, uow_factory, parts, alice)
        first = await listen(client, reply)
        again = await listen(client, reply)
        assert again.frames == first.frames and again.kinds[0] == "message_start"


class TestAfterTheStreamExpired:
    async def drop_stream(self, redis: Redis, env: str, reply: Reply) -> None:
        await redis.delete(redis_keys.stream(env, UUID(reply.message)))

    async def test_the_reply_is_replayed_from_the_database(
        self,
        client: httpx.AsyncClient,
        uow_factory: UowFactory,
        parts: Parts,
        alice: Session,
        redis_client: Redis,
        redis_env: str,
    ) -> None:
        reply = await ask(client, uow_factory, parts, alice)
        await self.drop_stream(redis_client, redis_env, reply)

        got = await listen(client, reply)

        got.check()
        assert got.kinds == ["message_start", "token", "citation", "message_complete"]
        assert got.last()["content"] == GOOD_ANSWER and got.last()["status"] == "completed"
        assert got.frames[1][2]["text"] == GOOD_ANSWER
        assert got.ids == ["0-1", "0-2", "0-3", "0-4"]

    async def test_a_replay_can_be_resumed_by_its_own_ids(
        self,
        client: httpx.AsyncClient,
        uow_factory: UowFactory,
        parts: Parts,
        alice: Session,
        redis_client: Redis,
        redis_env: str,
    ) -> None:
        reply = await ask(client, uow_factory, parts, alice)
        await self.drop_stream(redis_client, redis_env, reply)
        rest = await listen(client, reply, last_event_id="0-2")
        assert rest.kinds == ["citation", "message_complete"]

    async def test_a_listener_that_was_on_the_redis_stream_gets_the_whole_replay(
        self,
        client: httpx.AsyncClient,
        uow_factory: UowFactory,
        parts: Parts,
        alice: Session,
        redis_client: Redis,
        redis_env: str,
    ) -> None:
        reply = await ask(client, uow_factory, parts, alice)
        live = await listen(client, reply)
        await self.drop_stream(redis_client, redis_env, reply)

        again = await listen(client, reply, last_event_id=live.ids[2])  # a real redis id

        again.check()  # nothing is lost: message_start resets the client, content is final
        assert again.kinds[0] == "message_start" and again.kinds[-1] == "message_complete"

    async def test_a_failed_reply_replays_as_an_error(
        self,
        client: httpx.AsyncClient,
        uow_factory: UowFactory,
        parts: Parts,
        alice: Session,
        redis_client: Redis,
        redis_env: str,
    ) -> None:
        reply = await ask(client, uow_factory, parts, alice)
        async with uow_factory() as uow:
            row = (
                await uow.session.execute(select(Message).where(Message.id == UUID(reply.message)))
            ).scalar_one()
            row.status = MessageStatus.FAILED
            await uow.commit()
        await self.drop_stream(redis_client, redis_env, reply)

        got = await listen(client, reply)

        got.check()
        assert got.kinds[-1] == "error" and got.last()["code"] == "GENERATION_FAILED"
        assert got.last()["retryable"] is True


class TestAKilledProducer:
    async def test_the_listener_is_told_the_stream_stalled_and_the_sweeper_marks_it_interrupted(
        self,
        client: httpx.AsyncClient,
        uow_factory: UowFactory,
        parts: Parts,
        alice: Session,
    ) -> None:
        # A reply that was accepted but whose producer never ran (killed with the process).
        parts.retriever.gate = asyncio.Event()
        reply = await ask(client, uow_factory, parts, alice, wait=False)
        for task in list(parts.service._producers):
            task.cancel()
        await asyncio.gather(*parts.service._producers, return_exceptions=True)
        async with uow_factory() as uow:  # as if the process had died: no tidy interrupted state
            row = (
                await uow.session.execute(select(Message).where(Message.id == UUID(reply.message)))
            ).scalar_one()
            row.status = MessageStatus.STREAMING
            await uow.commit()

        stalled = await listen(client, reply)

        assert stalled.kinds[-1] == "error"
        assert stalled.last()["code"] == "STREAM_STALLED" and stalled.last()["retryable"] is True

        assert await parts.service.sweep_stale() == 1
        after = await listen(client, reply)
        assert after.last()["code"] == "INTERRUPTED"


class TestAuthorization:
    async def test_only_the_owner_can_listen(
        self,
        client: httpx.AsyncClient,
        uow_factory: UowFactory,
        parts: Parts,
        alice: Session,
        make_session: Make,
    ) -> None:
        reply = await ask(client, uow_factory, parts, alice)
        mallory: Session = await make_session()

        other = await client.get(reply.url, headers=mallory.headers)
        anonymous = await client.get(reply.url)

        assert other.status_code == 404 and other.json()["error"]["code"] == "NOT_FOUND"
        assert anonymous.status_code == 401

    async def test_the_message_must_belong_to_the_conversation_and_be_an_answer(
        self,
        client: httpx.AsyncClient,
        uow_factory: UowFactory,
        parts: Parts,
        alice: Session,
    ) -> None:
        reply = await ask(client, uow_factory, parts, alice)
        scope = OwnershipScope(UUID(alice.id))
        elsewhere = await client.post(
            "/api/v1/conversations", json={"title": "Other"}, headers=alice.headers
        )
        wrong_conversation = (
            f"/api/v1/conversations/{elsewhere.json()['id']}/stream?message_id={reply.message}"
        )
        listing = await client.get(
            f"/api/v1/conversations/{reply.conversation}/messages", headers=alice.headers
        )
        question = listing.json()["items"][0]["id"]
        user_message = f"/api/v1/conversations/{reply.conversation}/stream?message_id={question}"
        unknown = f"/api/v1/conversations/{reply.conversation}/stream?message_id={uuid4()}"

        for path in (wrong_conversation, user_message, unknown):
            response = await client.get(path, headers=alice.headers)
            assert response.status_code == 404, path
        assert scope  # the user's own answer is still reachable
        assert (await client.get(reply.url, headers=alice.headers)).status_code == 200

    async def test_a_missing_message_id_is_a_validation_error(
        self, client: httpx.AsyncClient, alice: Session
    ) -> None:
        response = await client.get(
            f"/api/v1/conversations/{uuid4()}/stream", headers=alice.headers
        )
        assert response.status_code == 422


class TestRedisTrouble:
    async def test_a_reply_still_completes_in_the_database_when_publishing_fails(
        self,
        client: httpx.AsyncClient,
        uow_factory: UowFactory,
        parts: Parts,
        alice: Session,
        redis_client: Redis,
        redis_env: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def broken(*args: object, **kwargs: object) -> None:
            raise RedisError("stream unavailable")

        monkeypatch.setattr(redis_client, "xadd", broken)

        reply = await ask(client, uow_factory, parts, alice)

        async with uow_factory() as uow:
            row = (
                await uow.session.execute(select(Message).where(Message.id == UUID(reply.message)))
            ).scalar_one()
        assert row.status is MessageStatus.COMPLETED and row.content == GOOD_ANSWER
        # and the listener, with no stream, is served from the database
        got = await listen(client, reply)
        got.check()
        assert got.last()["content"] == GOOD_ANSWER


class TestTheStreamKey:
    async def test_the_stream_has_a_short_expiry_once_the_reply_is_over(
        self,
        client: httpx.AsyncClient,
        uow_factory: UowFactory,
        parts: Parts,
        alice: Session,
        redis_client: Redis,
        redis_env: str,
    ) -> None:
        reply = await ask(client, uow_factory, parts, alice)
        ttl = await redis_client.ttl(redis_keys.stream(redis_env, UUID(reply.message)))
        assert 0 < ttl <= 600

    async def test_tokens_are_fewer_writes_than_tokens_generated(
        self,
        publisher: RedisStreamPublisher,
        redis_client: Redis,
        redis_env: str,
    ) -> None:
        mid = uuid4()
        cid = uuid4()

        def ev(kind: str, **fields: object) -> Any:
            return parse_event({"event": kind, "message_id": mid, **fields})

        await publisher.publish(mid, ev("message_start", conversation_id=cid, seq=2))
        for i in range(200):
            await publisher.publish(mid, ev("token", text="word ", index=i))
        await publisher.publish(
            mid, ev("message_complete", status="completed", grounding_status=None, content="")
        )

        entries = await redis_client.xrange(redis_keys.stream(redis_env, mid))
        texts = [json.loads(f[b"data"])["text"] for _, f in entries if f[b"event"] == b"token"]
        assert len(texts) < 40 and "".join(texts) == "word " * 200
