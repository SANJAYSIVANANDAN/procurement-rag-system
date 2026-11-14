"""A chat reply on real PostgreSQL and Redis: what is stored, what is locked, and what survives a
crash. The model is a scripted fake and the retriever returns a prepared result (retrieval itself is
tested against pgvector in its own suite)."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select

from covenantiq.core.enums import GroundingStatus, MessageStatus
from covenantiq.core.errors import GenerationInProgress, NotFound, TransientError, ValidationFailed
from covenantiq.db.models import Citation
from covenantiq.services.dto import Principal
from covenantiq.streaming.events import check_order
from tests.integration.chat.stack import Chat, UowFactory, build_chat
from tests.integration.memory.helpers import new_user
from tests.unit.graphs.harness import GOOD_ANSWER, QUESTION


@pytest.fixture
async def chat(uow_factory: UowFactory, redis_client: Redis, redis_env: str) -> Chat:
    return await build_chat(uow_factory, redis_client, redis_env)


async def ask(chat: Chat, text: str = QUESTION, **kwargs: object):  # type: ignore[no-untyped-def]
    submission = await chat.service.submit(
        chat.principal, chat.conversation_id, content=text, **kwargs
    )  # type: ignore[arg-type]
    await chat.service.idle()
    return submission


class TestAReply:
    async def test_the_reply_is_stored_with_its_citations_and_the_conversation_advances(
        self, chat: Chat
    ) -> None:
        submission = await ask(chat)

        assert not submission.replay and submission.status is MessageStatus.PENDING
        user, reply = await chat.messages()
        assert (user.seq, reply.seq) == (1, 2) and user.content == QUESTION
        assert reply.id == submission.assistant_message_id and reply.reply_to_id == user.id
        assert reply.status is MessageStatus.COMPLETED
        assert reply.grounding_status is GroundingStatus.GROUNDED
        assert reply.content == GOOD_ANSWER and reply.completed_at is not None
        assert reply.usage and reply.usage["output_tokens"] > 0
        assert reply.retrieval_trace and reply.retrieval_trace["graph"] == "chat_graph.v1"

        async with chat.uow_factory() as uow:
            rows = list((await uow.session.execute(select(Citation))).scalars().all())
        assert [
            (c.message_id, c.marker, c.document_title, c.clause_ref, c.validated) for c in rows
        ] == [(reply.id, 1, "Acme MSA", "6.2", True)]
        assert rows[0].chunk_id is not None and rows[0].document_id == chat.document_id

        assert (await chat.conversation()).last_completed_seq == 2

    async def test_the_events_obey_the_ordering_guarantee_and_the_lock_is_released(
        self, chat: Chat
    ) -> None:
        submission = await ask(chat)
        events = chat.publisher.of(submission.assistant_message_id)
        check_order(events)
        assert events[-1].event == "message_complete"
        assert not await chat.lock_held()

    async def test_the_exchange_is_in_the_redis_window_for_the_next_question(
        self, chat: Chat
    ) -> None:
        await ask(chat)
        await ask(chat, "And what about fees?")

        second_answer = [r for r in chat.llm.requests if r.route == "answer"][-1]
        assert [m.role for m in second_answer.messages] == ["user", "assistant", "user"]
        assert second_answer.messages[0].content == QUESTION
        assert second_answer.messages[1].content == GOOD_ANSWER

    async def test_a_question_that_finds_nothing_is_stored_as_an_abstention(
        self, chat: Chat
    ) -> None:
        chat.retriever.ready = chat.weak
        submission = await ask(chat)
        _, reply = await chat.messages()
        assert reply.id == submission.assistant_message_id
        assert reply.status is MessageStatus.COMPLETED
        assert reply.grounding_status is GroundingStatus.ABSTAINED
        assert (await chat.conversation()).last_completed_seq == 2
        async with chat.uow_factory() as uow:
            assert (
                await uow.session.execute(select(func.count()).select_from(Citation))
            ).scalar_one() == 0

    async def test_a_model_failure_leaves_a_failed_message_with_the_partial_text(
        self, chat: Chat
    ) -> None:
        chat.llm.fail_stream_after(4)
        submission = await ask(chat)

        _, reply = await chat.messages()
        assert reply.status is MessageStatus.FAILED and reply.content.strip() != ""
        assert GOOD_ANSWER.startswith(reply.content.strip().rsplit(" ", 1)[0])
        assert (await chat.conversation()).last_completed_seq == 0  # a failure is not a turn
        assert chat.publisher.of(submission.assistant_message_id)[-1].event == "error"
        assert not await chat.lock_held()

        # and the user can simply ask again
        chat.llm.heal()
        await ask(chat)
        assert (await chat.conversation()).last_completed_seq == 4

    async def test_documents_can_be_named_per_message(self, chat: Chat) -> None:
        await ask(chat, document_ids=[chat.document_id])
        assert list(chat.retriever.requests[0].document_ids) == [chat.document_id]

    async def test_naming_a_document_that_is_not_ready_is_a_validation_error_and_frees_the_lock(
        self, chat: Chat
    ) -> None:
        with pytest.raises(ValidationFailed):
            await chat.service.submit(
                chat.principal, chat.conversation_id, content="Q?", document_ids=[uuid4()]
            )
        assert not await chat.lock_held()
        assert await chat.messages() == []  # nothing stored

    async def test_an_empty_message_is_refused_and_frees_the_lock(self, chat: Chat) -> None:
        with pytest.raises(ValidationFailed):
            await chat.service.submit(chat.principal, chat.conversation_id, content="   ")
        assert not await chat.lock_held()


class TestIdempotency:
    async def test_the_same_client_message_id_returns_the_original_and_generates_nothing(
        self, chat: Chat
    ) -> None:
        client_id = uuid4()
        first = await ask(chat, client_message_id=client_id)
        calls = chat.llm.calls

        again = await ask(chat, client_message_id=client_id)

        assert again.replay and again.status is MessageStatus.COMPLETED
        assert (again.user_message_id, again.assistant_message_id) == (
            first.user_message_id,
            first.assistant_message_id,
        )
        assert chat.llm.calls == calls and len(await chat.messages()) == 2
        assert not await chat.lock_held()

    async def test_a_replay_is_answered_even_while_another_reply_is_being_written(
        self, chat: Chat
    ) -> None:
        client_id = uuid4()
        first = await ask(chat, client_message_id=client_id)
        chat.retriever.gate = asyncio.Event()
        await chat.service.submit(chat.principal, chat.conversation_id, content="Another one?")
        await asyncio.sleep(0.05)  # that reply now holds the lock

        again = await chat.service.submit(
            chat.principal, chat.conversation_id, content=QUESTION, client_message_id=client_id
        )
        assert again.replay and again.assistant_message_id == first.assistant_message_id

        chat.retriever.gate.set()
        await chat.service.idle()

    async def test_simultaneous_identical_submissions_produce_one_reply(self, chat: Chat) -> None:
        client_id = uuid4()
        outcomes = await asyncio.gather(
            *[
                chat.service.submit(
                    chat.principal,
                    chat.conversation_id,
                    content=QUESTION,
                    client_message_id=client_id,
                )
                for _ in range(4)
            ],
            return_exceptions=True,
        )
        await chat.service.idle()

        started = [o for o in outcomes if not isinstance(o, Exception) and not o.replay]
        assert len(started) == 1
        assert all(
            isinstance(o, GenerationInProgress) or o.replay for o in outcomes if o is not started[0]
        )  # type: ignore[union-attr]
        assert len(await chat.messages()) == 2 and chat.llm.calls == 2  # classify + answer


class TestOneReplyAtATime:
    async def test_a_second_question_while_one_is_generating_is_a_409_and_stores_nothing(
        self, chat: Chat
    ) -> None:
        chat.retriever.gate = asyncio.Event()
        await chat.service.submit(chat.principal, chat.conversation_id, content="First?")
        await asyncio.sleep(0.05)

        with pytest.raises(GenerationInProgress):
            await chat.service.submit(chat.principal, chat.conversation_id, content="Second?")
        assert len(await chat.messages()) == 2  # only the first pair

        chat.retriever.gate.set()
        await chat.service.idle()
        assert not await chat.lock_held()

        await ask(chat, "Second?")  # now it is free
        assert len(await chat.messages()) == 4

    async def test_other_conversations_are_not_blocked(
        self, chat: Chat, uow_factory: UowFactory
    ) -> None:
        chat.retriever.gate = asyncio.Event()
        await chat.service.submit(chat.principal, chat.conversation_id, content="First?")
        await asyncio.sleep(0.05)
        other = await chat.conversations.create(chat.principal, title="Other")

        await chat.service.submit(chat.principal, other.id, content="Different conversation?")

        chat.retriever.gate.set()
        await chat.service.idle()

    async def test_a_stranger_gets_a_404_never_a_409(
        self, chat: Chat, uow_factory: UowFactory
    ) -> None:
        stranger = Principal(user_id=(await new_user(uow_factory)).owner_id, email="x@example.com")
        chat.retriever.gate = asyncio.Event()
        await chat.service.submit(chat.principal, chat.conversation_id, content="First?")
        await asyncio.sleep(0.05)

        with pytest.raises(NotFound):  # not "a reply is in progress": that would leak
            await chat.service.submit(stranger, chat.conversation_id, content="Hello?")

        chat.retriever.gate.set()
        await chat.service.idle()

    async def test_the_lock_is_held_for_the_whole_reply_and_kept_alive(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str
    ) -> None:
        chat = await build_chat(
            uow_factory, redis_client, redis_env, lock_ttl=2, heartbeat_seconds=0.2
        )
        chat.retriever.gate = asyncio.Event()
        await chat.service.submit(chat.principal, chat.conversation_id, content="Slow one?")
        await asyncio.sleep(2.6)  # longer than the lock's lifetime without a heartbeat
        assert await chat.lock_held()
        with pytest.raises(GenerationInProgress):
            await chat.service.submit(chat.principal, chat.conversation_id, content="Again?")

        chat.retriever.gate.set()
        await chat.service.idle()
        assert not await chat.lock_held()


class TestRedisDown:
    async def test_chat_is_unavailable_and_nothing_is_stored(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str
    ) -> None:
        dead = Redis.from_url(
            "redis://127.0.0.1:1/0", socket_timeout=0.3, socket_connect_timeout=0.3
        )
        chat = await build_chat(uow_factory, redis_client, redis_env, lock_redis=dead)
        try:
            with pytest.raises(TransientError):
                await chat.service.submit(chat.principal, chat.conversation_id, content="Hello?")
            assert await chat.messages() == []  # the lock comes first, so nothing was written
        finally:
            await dead.aclose()


class TestShutdownAndCrashes:
    async def test_a_reply_still_running_at_shutdown_is_marked_interrupted(
        self, chat: Chat
    ) -> None:
        chat.retriever.gate = asyncio.Event()  # never released
        submission = await chat.service.submit(
            chat.principal, chat.conversation_id, content="Slow one?"
        )
        await asyncio.sleep(0.05)

        await chat.service.aclose(wait_seconds=0.1)

        _, reply = await chat.messages()
        assert reply.status is MessageStatus.INTERRUPTED and reply.completed_at is not None
        events = chat.publisher.of(submission.assistant_message_id)
        check_order(events)
        assert events[-1].event == "error" and events[-1].code == "INTERRUPTED"  # type: ignore[union-attr]
        assert not await chat.lock_held()

    async def test_a_reply_that_finishes_during_shutdown_is_left_alone(self, chat: Chat) -> None:
        await chat.service.submit(chat.principal, chat.conversation_id, content=QUESTION)
        await chat.service.aclose(wait_seconds=10)
        _, reply = await chat.messages()
        assert reply.status is MessageStatus.COMPLETED

    async def test_the_sweeper_interrupts_only_replies_nobody_is_writing(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str
    ) -> None:
        chat = await build_chat(uow_factory, redis_client, redis_env)
        # a pending pair left by a process that died: store it without starting a reply
        submitted = await chat.conversations.submit_message(
            chat.principal, chat.conversation_id, content="Orphan?"
        )

        assert await chat.service.sweep_stale() == 0  # too fresh to judge

        strict = await build_chat(
            uow_factory, redis_client, redis_env, stale_after=timedelta(seconds=-5)
        )
        assert await strict.service.sweep_stale() >= 1
        stored = {m.id: m for m in await chat.messages()}
        assert stored[submitted.assistant_message_id].status is MessageStatus.INTERRUPTED
        assert (
            stored[submitted.user_message_id].status is MessageStatus.COMPLETED
        )  # the question stays

    async def test_a_late_failure_cannot_overwrite_a_finished_reply(self, chat: Chat) -> None:
        submission = await ask(chat)
        from covenantiq.graphs.deps import ChatOutcome
        from covenantiq.services.chat_persistence import PostgresChatStore

        store = PostgresChatStore(chat.uow_factory)
        await store.complete(
            chat.principal.scope(),
            submission.assistant_message_id,
            ChatOutcome("", MessageStatus.FAILED, None, None),
        )
        _, reply = await chat.messages()
        assert reply.status is MessageStatus.COMPLETED and reply.content == GOOD_ANSWER
