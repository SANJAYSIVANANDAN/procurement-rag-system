from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from covenantiq.core.enums import GroundingStatus, MessageStatus
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from tests.integration.factories import (
    make_conversation,
    make_document,
    make_message_pair,
    make_user,
)

UowFactory = Callable[[], UnitOfWork]


async def two_scopes(uow_factory: UowFactory) -> tuple[OwnershipScope, OwnershipScope]:
    async with uow_factory() as uow:
        alice = await uow.users.add(make_user())
        bob = await uow.users.add(make_user())
        await uow.commit()
    return OwnershipScope(alice.id), OwnershipScope(bob.id)


async def submit(uow_factory: UowFactory, scope: OwnershipScope, conversation_id, **kwargs):  # type: ignore[no-untyped-def]
    """What ChatService.submit_message will do: lock, allocate seq numbers, store the pair."""
    async with uow_factory() as uow:
        locked = await uow.conversations.get_for_update(scope, conversation_id)
        assert locked is not None
        user_message, reply = await uow.messages.add_pair(
            scope, locked, *make_message_pair(**kwargs)
        )
        await uow.commit()
    return user_message, reply


async def test_sequence_numbers_and_reply_link(uow_factory: UowFactory) -> None:
    alice, _ = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        conversation = await uow.conversations.create(alice, make_conversation(alice.owner_id))
        await uow.commit()

    first_user, first_reply = await submit(uow_factory, alice, conversation.id)
    second_user, second_reply = await submit(uow_factory, alice, conversation.id)

    assert [m.seq for m in (first_user, first_reply, second_user, second_reply)] == [1, 2, 3, 4]
    assert first_reply.reply_to_id == first_user.id
    async with uow_factory() as uow:
        stored = await uow.conversations.get(alice, conversation.id)
        assert stored is not None
        assert stored.last_message_seq == 4


async def test_idempotency_lookup_by_client_message_id(uow_factory: UowFactory) -> None:
    alice, bob = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        conversation = await uow.conversations.create(alice, make_conversation(alice.owner_id))
        await uow.commit()
    client_id = uuid4()
    user_message, _ = await submit(uow_factory, alice, conversation.id, client_message_id=client_id)

    async with uow_factory() as uow:
        found = await uow.messages.get_by_client_id(alice, conversation.id, client_id)
        assert found is not None
        assert found.id == user_message.id
        assert await uow.messages.get_by_client_id(alice, conversation.id, uuid4()) is None
        assert await uow.messages.get_by_client_id(bob, conversation.id, client_id) is None


async def test_recent_returns_the_newest_messages_oldest_first(uow_factory: UowFactory) -> None:
    alice, bob = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        conversation = await uow.conversations.create(alice, make_conversation(alice.owner_id))
        await uow.commit()
    for _ in range(3):
        await submit(uow_factory, alice, conversation.id)

    async with uow_factory() as uow:
        assert [m.seq for m in await uow.messages.recent(alice, conversation.id, 4)] == [3, 4, 5, 6]
        assert [
            m.seq for m in await uow.messages.recent(alice, conversation.id, 2, before_seq=4)
        ] == [
            2,
            3,
        ]
        assert await uow.messages.recent(bob, conversation.id, 10) == []
        assert await uow.messages.list(bob, conversation.id) == []


async def test_completing_a_reply_advances_last_completed_seq(uow_factory: UowFactory) -> None:
    alice, bob = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        conversation = await uow.conversations.create(alice, make_conversation(alice.owner_id))
        await uow.commit()
    _, reply = await submit(uow_factory, alice, conversation.id)

    async with uow_factory() as uow:
        assert not await uow.messages.complete(
            bob, reply.id, content="hijack", status=MessageStatus.COMPLETED
        )
        assert await uow.messages.complete(
            alice,
            reply.id,
            content="Ninety days.",
            status=MessageStatus.COMPLETED,
            grounding_status=GroundingStatus.GROUNDED,
            retrieval_trace={"chunk_ids": ["c1"]},
        )
        await uow.commit()

    async with uow_factory() as uow:
        stored = await uow.messages.get(alice, reply.id)
        assert stored is not None
        assert stored.content == "Ninety days."
        assert stored.completed_at is not None
        assert stored.grounding_status is GroundingStatus.GROUNDED
        conversation_row = await uow.conversations.get(alice, conversation.id)
        assert conversation_row is not None
        assert conversation_row.last_completed_seq == 2


async def test_failed_reply_does_not_advance_last_completed_seq(uow_factory: UowFactory) -> None:
    alice, _ = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        conversation = await uow.conversations.create(alice, make_conversation(alice.owner_id))
        await uow.commit()
    _, reply = await submit(uow_factory, alice, conversation.id)
    async with uow_factory() as uow:
        await uow.messages.complete(alice, reply.id, content="", status=MessageStatus.FAILED)
        await uow.commit()
    async with uow_factory() as uow:
        row = await uow.conversations.get(alice, conversation.id)
        assert row is not None
        assert row.last_completed_seq == 0


async def test_stale_pending_messages_are_marked_interrupted(uow_factory: UowFactory) -> None:
    alice, _ = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        conversation = await uow.conversations.create(alice, make_conversation(alice.owner_id))
        await uow.commit()
    _, reply = await submit(uow_factory, alice, conversation.id)

    async with uow_factory() as uow:
        future = datetime.now(UTC) + timedelta(minutes=10)
        assert await uow.messages.system_mark_interrupted_stale(future) == 1
        assert await uow.messages.system_mark_interrupted_stale(future) == 0
        await uow.commit()
    async with uow_factory() as uow:
        stored = await uow.messages.get(alice, reply.id)
        assert stored is not None
        assert stored.status is MessageStatus.INTERRUPTED


async def test_summary_and_document_scope(uow_factory: UowFactory) -> None:
    alice, bob = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        conversation = await uow.conversations.create(alice, make_conversation(alice.owner_id))
        mine = await uow.documents.add(alice, make_document(alice.owner_id))
        deleted = await uow.documents.add(alice, make_document(alice.owner_id))
        theirs = await uow.documents.add(bob, make_document(bob.owner_id))
        await uow.documents.soft_delete(alice, deleted.id)
        await uow.commit()

    async with uow_factory() as uow:
        stored = await uow.conversations.set_documents(
            alice, conversation.id, [mine.id, deleted.id, theirs.id]
        )
        assert list(stored) == [mine.id]
        assert await uow.conversations.set_documents(bob, conversation.id, [theirs.id]) == []
        assert await uow.conversations.update_summary(alice, conversation.id, "Summary.", 4)
        assert not await uow.conversations.update_summary(bob, conversation.id, "x", 1)
        await uow.commit()

    async with uow_factory() as uow:
        assert list(await uow.conversations.document_ids(alice, conversation.id)) == [mine.id]
        assert list(await uow.conversations.document_ids(bob, conversation.id)) == []
        row = await uow.conversations.get(alice, conversation.id)
        assert row is not None
        assert (row.summary, row.summary_through_seq) == ("Summary.", 4)


async def test_conversations_are_listed_per_owner_and_archivable(uow_factory: UowFactory) -> None:
    alice, bob = await two_scopes(uow_factory)
    async with uow_factory() as uow:
        keep = await uow.conversations.create(alice, make_conversation(alice.owner_id))
        archived = await uow.conversations.create(
            alice, make_conversation(alice.owner_id, archived=True)
        )
        await uow.conversations.create(bob, make_conversation(bob.owner_id))
        await uow.commit()
    async with uow_factory() as uow:
        assert [c.id for c in await uow.conversations.list(alice)] == [keep.id]
        listed = await uow.conversations.list(alice, include_archived=True)
        assert {c.id for c in listed} == {keep.id, archived.id}
        assert await uow.conversations.get(bob, keep.id) is None
