"""The conversation service against PostgreSQL: CRUD, history with citations, and the submit-side
persistence of messages (sequence numbers under concurrency, idempotent replay, document scope)."""

import asyncio
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from covenantiq.core.enums import DocumentStatus, MessageRole, MessageStatus
from covenantiq.core.errors import Conflict, NotFound, ValidationFailed
from covenantiq.db.models import Citation
from covenantiq.db.uow import UnitOfWork
from covenantiq.services.conversation_service import MAX_MESSAGE_CHARS, ConversationService
from covenantiq.services.dto import Principal
from tests.integration.factories import make_document, make_user, make_vendor

UowFactory = Callable[[], UnitOfWork]


async def new_principal(uow_factory: UowFactory, email: str | None = None) -> Principal:
    async with uow_factory() as uow:
        user = await uow.users.add(make_user(email))
        await uow.commit()
    return Principal(user_id=user.id, email=user.email)


async def new_document(
    uow_factory: UowFactory, principal: Principal, status: DocumentStatus = DocumentStatus.READY
) -> UUID:
    async with uow_factory() as uow:
        document = await uow.documents.add(
            principal.scope(), make_document(principal.user_id, status=status)
        )
        await uow.commit()
    return document.id


@pytest.fixture
def service(uow_factory: UowFactory) -> ConversationService:
    return ConversationService(uow_factory)


class TestCreateAndRead:
    async def test_a_conversation_is_created_for_its_owner(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory)
        created = await service.create(alice, title="  Liability   review ")
        assert created.title == "Liability review"  # whitespace normalised
        assert (created.archived, created.last_message_seq, created.document_ids) == (False, 0, [])
        assert (await service.get(alice, created.id)).conversation.id == created.id

    async def test_the_document_scope_keeps_only_the_users_own_documents(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory, "alice@example.com")
        bob = await new_principal(uow_factory, "bob@example.com")
        mine = await new_document(uow_factory, alice)
        theirs = await new_document(uow_factory, bob)
        created = await service.create(alice, document_ids=[mine, theirs, uuid4()])
        assert created.document_ids == [mine]
        detail = await service.get(alice, created.id)
        assert [d.id for d in detail.documents] == [mine]

    async def test_a_vendor_the_user_does_not_own_is_refused(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory, "alice@example.com")
        bob = await new_principal(uow_factory, "bob@example.com")
        async with uow_factory() as uow:
            vendor = await uow.vendors.add(bob.scope(), make_vendor(bob.user_id))
            await uow.commit()
        with pytest.raises(NotFound):
            await service.create(alice, vendor_id=vendor.id)

    async def test_someone_elses_conversation_does_not_exist(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory, "alice@example.com")
        bob = await new_principal(uow_factory, "bob@example.com")
        created = await service.create(alice)
        for call in (
            service.get(bob, created.id),
            service.update(bob, created.id, {"title": "mine now"}),
            service.messages(bob, created.id),
            service.submit_message(bob, created.id, content="hello"),
        ):
            with pytest.raises(NotFound):
                await call
        with pytest.raises(NotFound):
            await service.get(alice, uuid4())

    async def test_an_overlong_title_is_refused(
        self, service: ConversationService, uow_factory: UowFactory
    ) -> None:
        alice = await new_principal(uow_factory)
        with pytest.raises(ValidationFailed):
            await service.create(alice, title="x" * 201)


class TestListing:
    async def test_newest_activity_first_with_a_cursor_that_walks_the_whole_list(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory)
        ids = [(await service.create(alice, title=f"c{i}")).id for i in range(5)]
        seen: list[UUID] = []
        cursor = None
        pages = 0
        while True:
            page = await service.list(alice, limit=2, cursor=cursor)
            seen.extend(item.id for item in page.items)
            pages += 1
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == list(reversed(ids))  # newest first, none repeated, none missed
        assert pages == 3

    async def test_archived_conversations_are_hidden_unless_asked_for(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory)
        kept = await service.create(alice, title="kept")
        old = await service.create(alice, title="old")
        await service.update(alice, old.id, {"archived": True})
        assert [c.id for c in (await service.list(alice)).items] == [kept.id]
        assert {c.id for c in (await service.list(alice, include_archived=True)).items} == {
            kept.id,
            old.id,
        }
        await service.update(alice, old.id, {"archived": False})  # restored
        assert len((await service.list(alice)).items) == 2

    async def test_each_user_sees_only_their_own(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory, "alice@example.com")
        bob = await new_principal(uow_factory, "bob@example.com")
        await service.create(alice)
        assert (await service.list(bob)).items == []

    async def test_a_forged_cursor_is_refused(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory)
        with pytest.raises(ValidationFailed):
            await service.list(alice, cursor="not-a-cursor")


class TestUpdate:
    async def test_only_the_fields_sent_change(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory)
        doc = await new_document(uow_factory, alice)
        created = await service.create(alice, title="Original", document_ids=[doc])
        renamed = await service.update(alice, created.id, {"title": "Renamed"})
        assert renamed.title == "Renamed" and renamed.document_ids == [doc]
        cleared = await service.update(alice, created.id, {"document_ids": []})
        assert cleared.document_ids == [] and cleared.title == "Renamed"
        untitled = await service.update(alice, created.id, {"title": None})
        assert untitled.title is None

    async def test_unknown_fields_are_refused(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory)
        created = await service.create(alice)
        with pytest.raises(ValidationFailed):
            await service.update(alice, created.id, {"last_message_seq": 99})


class TestSubmit:
    async def conversation(self, uow_factory: UowFactory, service: ConversationService):  # type: ignore[no-untyped-def]
        alice = await new_principal(uow_factory)
        doc = await new_document(uow_factory, alice)
        created = await service.create(alice, document_ids=[doc])
        return alice, created.id, doc

    async def test_a_message_stores_a_user_message_and_a_pending_reply_with_the_next_two_seqs(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice, conversation, _ = await self.conversation(uow_factory, service)
        first = await service.submit_message(alice, conversation, content="  What is the cap?  ")
        second = await service.submit_message(alice, conversation, content="And carve-outs?")

        assert first.replay is False and first.assistant_status is MessageStatus.PENDING
        page = await service.messages(alice, conversation)
        assert [(m.seq, m.role, m.status) for m in page.items] == [
            (1, MessageRole.USER, MessageStatus.COMPLETED),
            (2, MessageRole.ASSISTANT, MessageStatus.PENDING),
            (3, MessageRole.USER, MessageStatus.COMPLETED),
            (4, MessageRole.ASSISTANT, MessageStatus.PENDING),
        ]
        assert page.items[0].content == "What is the cap?"  # trimmed
        assert page.items[1].id == first.assistant_message_id
        assert page.items[3].id == second.assistant_message_id

    async def test_the_resolved_documents_are_stored_on_both_messages(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice, conversation, doc = await self.conversation(uow_factory, service)
        submitted = await service.submit_message(alice, conversation, content="hello")
        assert submitted.selected_document_ids == [doc]
        async with uow_factory() as uow:
            rows = (
                await uow.session.execute(
                    text(
                        "SELECT selected_document_ids FROM messages "
                        "WHERE conversation_id = :c ORDER BY seq"
                    ),
                    {"c": conversation},
                )
            ).all()
        assert [r[0] for r in rows] == [[str(doc)], [str(doc)]]

    async def test_ten_concurrent_submissions_get_ten_distinct_consecutive_pairs(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice, conversation, _ = await self.conversation(uow_factory, service)
        results = await asyncio.gather(
            *(
                service.submit_message(alice, conversation, content=f"question {i}")
                for i in range(10)
            )
        )
        assert len({r.user_message_id for r in results}) == 10
        page = await service.messages(alice, conversation, limit=50)
        assert [m.seq for m in page.items] == list(range(1, 21))  # no gaps, no duplicates
        # each reply sits directly after its question
        by_id = {m.id: m for m in page.items}
        for result in results:
            assert by_id[result.assistant_message_id].seq == by_id[result.user_message_id].seq + 1

    async def test_the_same_client_message_id_is_stored_once_and_replayed(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice, conversation, _ = await self.conversation(uow_factory, service)
        key = uuid4()
        first = await service.submit_message(
            alice, conversation, content="hi", client_message_id=key
        )
        again = await service.submit_message(
            alice, conversation, content="hi", client_message_id=key
        )
        assert again.replay is True
        assert (again.user_message_id, again.assistant_message_id) == (
            first.user_message_id,
            first.assistant_message_id,
        )
        assert len((await service.messages(alice, conversation)).items) == 2

    async def test_a_double_click_race_still_stores_exactly_one_message(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice, conversation, _ = await self.conversation(uow_factory, service)
        key = uuid4()
        results = await asyncio.gather(
            *(
                service.submit_message(alice, conversation, content="hi", client_message_id=key)
                for _ in range(6)
            )
        )
        assert len({r.user_message_id for r in results}) == 1
        assert sum(1 for r in results if not r.replay) == 1
        assert len((await service.messages(alice, conversation)).items) == 2

    async def test_the_same_client_id_in_another_conversation_is_not_a_replay(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice, first, _ = await self.conversation(uow_factory, service)
        second = (await service.create(alice)).id
        key = uuid4()
        a = await service.submit_message(alice, first, content="hi", client_message_id=key)
        b = await service.submit_message(alice, second, content="hi", client_message_id=key)
        assert b.replay is False and a.user_message_id != b.user_message_id

    @pytest.mark.parametrize("content", ["", "   ", "\n\t"])
    async def test_an_empty_message_is_refused(
        self, uow_factory: UowFactory, service: ConversationService, content: str
    ) -> None:
        alice, conversation, _ = await self.conversation(uow_factory, service)
        with pytest.raises(ValidationFailed):
            await service.submit_message(alice, conversation, content=content)

    async def test_an_overlong_message_is_refused(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice, conversation, _ = await self.conversation(uow_factory, service)
        with pytest.raises(ValidationFailed):
            await service.submit_message(alice, conversation, content="x" * (MAX_MESSAGE_CHARS + 1))
        await service.submit_message(alice, conversation, content="x" * MAX_MESSAGE_CHARS)

    async def test_an_archived_conversation_must_be_restored_first(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice, conversation, _ = await self.conversation(uow_factory, service)
        await service.update(alice, conversation, {"archived": True})
        with pytest.raises(Conflict):
            await service.submit_message(alice, conversation, content="hello")
        await service.update(alice, conversation, {"archived": False})
        await service.submit_message(alice, conversation, content="hello")


class TestDocumentScope:
    async def test_named_documents_win_and_must_be_ready(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory)
        scoped, other = (
            await new_document(uow_factory, alice),
            await new_document(uow_factory, alice),
        )
        pending = await new_document(uow_factory, alice, DocumentStatus.QUEUED)
        conversation = (await service.create(alice, document_ids=[scoped])).id

        named = await service.submit_message(
            alice, conversation, content="q", document_ids=[other, pending]
        )
        assert named.selected_document_ids == [other]  # the unfinished one is dropped
        with pytest.raises(ValidationFailed):
            await service.submit_message(alice, conversation, content="q", document_ids=[pending])

    async def test_another_users_document_id_is_never_searched(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory, "alice@example.com")
        bob = await new_principal(uow_factory, "bob@example.com")
        mine = await new_document(uow_factory, alice)
        theirs = await new_document(uow_factory, bob)
        conversation = (await service.create(alice)).id
        submitted = await service.submit_message(
            alice, conversation, content="q", document_ids=[mine, theirs]
        )
        assert submitted.selected_document_ids == [mine]
        with pytest.raises(ValidationFailed):
            await service.submit_message(alice, conversation, content="q", document_ids=[theirs])

    async def test_without_a_scope_every_ready_document_is_searched(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory)
        ready = {await new_document(uow_factory, alice) for _ in range(3)}
        await new_document(uow_factory, alice, DocumentStatus.FAILED)
        conversation = (await service.create(alice)).id
        submitted = await service.submit_message(alice, conversation, content="q")
        assert set(submitted.selected_document_ids) == ready

    async def test_the_conversations_own_scope_is_used_when_none_is_named(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice = await new_principal(uow_factory)
        scoped = await new_document(uow_factory, alice)
        await new_document(uow_factory, alice)
        conversation = (await service.create(alice, document_ids=[scoped])).id
        submitted = await service.submit_message(alice, conversation, content="q")
        assert submitted.selected_document_ids == [scoped]


class TestHistory:
    async def seed_history(self, uow_factory: UowFactory, service: ConversationService, count: int):  # type: ignore[no-untyped-def]
        alice = await new_principal(uow_factory)
        doc = await new_document(uow_factory, alice)
        conversation = (await service.create(alice, document_ids=[doc])).id
        replies = []
        for i in range(count):
            submitted = await service.submit_message(alice, conversation, content=f"question {i}")
            async with uow_factory() as uow:
                await uow.messages.complete(
                    alice.scope(),
                    submitted.assistant_message_id,
                    content=f"answer {i}",
                    status=MessageStatus.COMPLETED,
                )
                await uow.commit()
            replies.append(submitted.assistant_message_id)
        return alice, conversation, doc, replies

    async def test_pages_walk_backwards_through_history_without_gaps(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice, conversation, _, _ = await self.seed_history(uow_factory, service, 5)  # 10 messages
        collected: list[int] = []
        before = None
        while True:
            page = await service.messages(alice, conversation, before_seq=before, limit=4)
            collected = [m.seq for m in page.items] + collected  # each page is older than the last
            if page.next_cursor is None:
                break
            before = int(page.next_cursor)
        assert collected == list(range(1, 11))

    async def test_citations_come_with_their_message(
        self, uow_factory: UowFactory, service: ConversationService
    ) -> None:
        alice, conversation, doc, replies = await self.seed_history(uow_factory, service, 1)
        async with uow_factory() as uow:
            await uow.citations.bulk_add(
                alice.scope(),
                [
                    Citation(
                        message_id=replies[0],
                        document_id=doc,
                        marker=2,
                        quote="second",
                        document_title="MSA",
                        page_start=14,
                        page_end=15,
                        clause_ref="12.3",
                        validated=True,
                    ),
                    Citation(
                        message_id=replies[0],
                        document_id=doc,
                        marker=1,
                        quote="first",
                        document_title="MSA",
                        page_start=14,
                        page_end=14,
                        clause_ref="12.2",
                        validated=False,
                    ),
                ],
            )
            await uow.commit()
        page = await service.messages(alice, conversation)
        user, assistant = page.items
        assert user.citations == []
        assert [(c.marker, c.quote, c.validated) for c in assistant.citations] == [
            (1, "first", False),
            (2, "second", True),
        ]
        assert assistant.citations[1].clause_ref == "12.3" and assistant.citations[1].page_end == 15
        assert assistant.content == "answer 0"
