"""Conversations: create, list, rename, scope, archive, read history, and the submit-side
persistence of a user message (spec 16.2, 24.3). Generation is not here: submitting stores the user
message and a pending assistant message in one transaction and stops; the chat graph (milestone 11)
takes over from there.

Every method starts from the principal and checks ownership in PostgreSQL first. Redis is only
reached afterwards, through `MemoryManager`, and never with an id the caller did not prove it owns.
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from covenantiq.core.enums import DocumentStatus, MessageRole, MessageStatus
from covenantiq.core.errors import Conflict, NotFound, ValidationFailed
from covenantiq.db.models import Conversation, Message
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.memory import ConversationState, MemoryContext, MemoryManager
from covenantiq.services.dto import (
    ConversationDetailDTO,
    ConversationDTO,
    MessageDTO,
    Page,
    Principal,
    SubmittedMessageDTO,
    conversation_to_dto,
    document_to_dto,
    message_to_dto,
)
from covenantiq.services.pagination import decode_cursor, encode_cursor

MAX_TITLE_LENGTH = 200
MAX_MESSAGE_CHARS = 8000
DEFAULT_SCOPE_LIMIT = 200  # most ready documents searched when no scope is given
PATCHABLE_FIELDS = frozenset({"title", "document_ids", "archived", "vendor_id"})


def clean_title(title: str | None) -> str | None:
    if title is None:
        return None
    cleaned = " ".join(title.split())
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise ValidationFailed(f"The title must be at most {MAX_TITLE_LENGTH} characters.")
    return cleaned or None


class ConversationService:
    def __init__(
        self, uow_factory: Callable[[], UnitOfWork], memory: MemoryManager | None = None
    ) -> None:
        self._uow_factory = uow_factory
        self._memory = memory

    # ---- conversations ----------------------------------------------------------------------

    async def create(
        self,
        principal: Principal,
        *,
        title: str | None = None,
        document_ids: Sequence[UUID] = (),
        vendor_id: UUID | None = None,
    ) -> ConversationDTO:
        scope = principal.scope()
        async with self._uow_factory() as uow:
            if vendor_id is not None and await uow.vendors.get(scope, vendor_id) is None:
                raise NotFound("Vendor not found.")
            conversation = await uow.conversations.create(
                scope,
                Conversation(
                    owner_id=scope.owner_id, title=clean_title(title), vendor_id=vendor_id
                ),
            )
            stored = list(
                await uow.conversations.set_documents(scope, conversation.id, document_ids)
            )
            await uow.commit()
            return conversation_to_dto(conversation, stored)

    async def list(
        self,
        principal: Principal,
        *,
        include_archived: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page[ConversationDTO]:
        after = None
        if cursor is not None:
            updated, conversation_id = decode_cursor(cursor, parts=2)
            try:
                after = (datetime.fromisoformat(updated), UUID(conversation_id))
            except ValueError as exc:
                raise ValidationFailed("The cursor is invalid.") from exc
        scope = principal.scope()
        async with self._uow_factory() as uow:
            rows = await uow.conversations.list(
                scope, include_archived=include_archived, limit=limit + 1, after=after
            )
            page = rows[:limit]
            items = [
                conversation_to_dto(row, list(await uow.conversations.document_ids(scope, row.id)))
                for row in page
            ]
        next_cursor = (
            encode_cursor(page[-1].updated_at.isoformat(), str(page[-1].id))
            if len(rows) > limit
            else None
        )
        return Page(items=items, next_cursor=next_cursor)

    async def get(self, principal: Principal, conversation_id: UUID) -> ConversationDetailDTO:
        scope = principal.scope()
        async with self._uow_factory() as uow:
            conversation = await self._owned(uow, scope, conversation_id)
            ids = list(await uow.conversations.document_ids(scope, conversation_id))
            documents = []
            for document_id in ids:
                document = await uow.documents.get(scope, document_id)
                if document is not None:
                    documents.append(document_to_dto(document))
            return ConversationDetailDTO(conversation_to_dto(conversation, ids), documents)

    async def update(
        self, principal: Principal, conversation_id: UUID, changes: dict[str, Any]
    ) -> ConversationDTO:
        """Rename, change the document scope, archive or restore. Only the fields sent change."""
        unknown = set(changes) - PATCHABLE_FIELDS
        if unknown:
            raise ValidationFailed(f"These fields cannot be changed: {', '.join(sorted(unknown))}.")
        scope = principal.scope()
        async with self._uow_factory() as uow:
            conversation = await self._owned(uow, scope, conversation_id)
            if "title" in changes:
                conversation.title = clean_title(changes["title"])
            if "archived" in changes:
                conversation.archived = bool(changes["archived"])
            if "vendor_id" in changes:
                vendor_id = changes["vendor_id"]
                if vendor_id is not None and await uow.vendors.get(scope, vendor_id) is None:
                    raise NotFound("Vendor not found.")
                conversation.vendor_id = vendor_id
            if "document_ids" in changes:
                await uow.conversations.set_documents(
                    scope, conversation_id, changes["document_ids"] or []
                )
            await uow.session.flush()
            await uow.session.refresh(conversation)
            ids = list(await uow.conversations.document_ids(scope, conversation_id))
            await uow.commit()
            return conversation_to_dto(conversation, ids)

    # ---- messages ---------------------------------------------------------------------------

    async def messages(
        self,
        principal: Principal,
        conversation_id: UUID,
        *,
        before_seq: int | None = None,
        limit: int = 50,
    ) -> Page[MessageDTO]:
        """The newest `limit` messages (older than `before_seq` if given), oldest first, with their
        citations. `next_cursor` is the `before_seq` for the next, older page."""
        scope = principal.scope()
        async with self._uow_factory() as uow:
            await self._owned(uow, scope, conversation_id)
            rows = await uow.messages.recent(
                scope, conversation_id, limit + 1, before_seq=before_seq
            )
            older_exists = len(rows) > limit
            page = list(rows[1:] if older_exists else rows)
            citations = await uow.citations.for_messages(scope, [m.id for m in page])
        by_message: dict[UUID, list[Any]] = {}
        for citation in citations:
            if citation.message_id is not None:
                by_message.setdefault(citation.message_id, []).append(citation)
        items = [message_to_dto(m, by_message.get(m.id, [])) for m in page]
        return Page(items=items, next_cursor=str(page[0].seq) if older_exists and page else None)

    async def submit_message(
        self,
        principal: Principal,
        conversation_id: UUID,
        *,
        content: str,
        client_message_id: UUID | None = None,
        document_ids: Sequence[UUID] | None = None,
    ) -> SubmittedMessageDTO:
        """Store a user message and a pending assistant message, atomically, and return both ids.

        Sequence numbers come from the conversation row, locked for the transaction, so concurrent
        submissions cannot interleave. Sending the same `client_message_id` again stores nothing and
        returns the original ids (an idempotent replay). Starting generation is the caller's job.
        """
        text = content.strip()
        if not text:
            raise ValidationFailed("The message must not be empty.")
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValidationFailed(f"The message must be at most {MAX_MESSAGE_CHARS} characters.")
        scope = principal.scope()
        try:
            return await self._submit(scope, conversation_id, text, client_message_id, document_ids)
        except IntegrityError:
            # two identical submissions raced; the other one committed first
            if client_message_id is None:
                raise
            replay = await self._replay(scope, conversation_id, client_message_id)
            if replay is None:
                raise
            return replay

    async def ensure_owned(self, principal: Principal, conversation_id: UUID) -> None:
        """Raise NotFound unless the conversation is the caller's. Called before anything else
        touches the conversation, so a stranger learns nothing about it (not even that it is busy).
        """
        async with self._uow_factory() as uow:
            await self._owned(uow, principal.scope(), conversation_id)

    async def existing_submission(
        self, principal: Principal, conversation_id: UUID, client_message_id: UUID
    ) -> SubmittedMessageDTO | None:
        """The earlier submission with this `client_message_id`, if there is one."""
        return await self._replay(principal.scope(), conversation_id, client_message_id)

    async def _submit(
        self,
        scope: OwnershipScope,
        conversation_id: UUID,
        text: str,
        client_message_id: UUID | None,
        document_ids: Sequence[UUID] | None,
    ) -> SubmittedMessageDTO:
        async with self._uow_factory() as uow:
            conversation = await uow.conversations.get_for_update(scope, conversation_id)
            if conversation is None:
                raise NotFound("Conversation not found.")
            if client_message_id is not None:
                existing = await uow.messages.get_by_client_id(
                    scope, conversation_id, client_message_id
                )
                if existing is not None:
                    reply = await uow.messages.get_reply(scope, existing.id)
                    if reply is not None:
                        return SubmittedMessageDTO(
                            existing.id,
                            reply.id,
                            reply.status,
                            [UUID(str(i)) for i in existing.selected_document_ids],
                            replay=True,
                        )
            if conversation.archived:
                raise Conflict("This conversation is archived; restore it to continue.")
            resolved = await self._resolve_documents(uow, scope, conversation_id, document_ids)
            user, assistant = await uow.messages.add_pair(
                scope,
                conversation,
                Message(
                    role=MessageRole.USER,
                    content=text,
                    status=MessageStatus.COMPLETED,
                    client_message_id=client_message_id,
                    selected_document_ids=[str(i) for i in resolved],
                ),
                Message(
                    role=MessageRole.ASSISTANT,
                    status=MessageStatus.PENDING,
                    selected_document_ids=[str(i) for i in resolved],
                ),
            )
            ids = (user.id, assistant.id)
            await uow.commit()
        return SubmittedMessageDTO(ids[0], ids[1], MessageStatus.PENDING, [*resolved], replay=False)

    async def _replay(
        self, scope: OwnershipScope, conversation_id: UUID, client_message_id: UUID
    ) -> SubmittedMessageDTO | None:
        async with self._uow_factory() as uow:
            existing = await uow.messages.get_by_client_id(
                scope, conversation_id, client_message_id
            )
            if existing is None:
                return None
            reply = await uow.messages.get_reply(scope, existing.id)
            if reply is None:
                return None
            return SubmittedMessageDTO(
                existing.id,
                reply.id,
                reply.status,
                [UUID(str(i)) for i in existing.selected_document_ids],
                replay=True,
            )

    @staticmethod
    async def _resolve_documents(
        uow: UnitOfWork,
        scope: OwnershipScope,
        conversation_id: UUID,
        requested: Sequence[UUID] | None,
    ) -> Sequence[UUID]:
        """Which documents this message searches (spec 19.2 step 3): the ones named, else the
        conversation's scope, else every ready document, always cut down to the user's own,
        ready, not-deleted documents."""
        if requested:
            wanted = list(dict.fromkeys(requested))
        else:
            wanted = list(await uow.conversations.document_ids(scope, conversation_id))
        ready = await uow.documents.list(
            scope, status=DocumentStatus.READY, limit=DEFAULT_SCOPE_LIMIT
        )
        ready_ids = [d.id for d in ready]
        if not wanted:
            return ready_ids
        resolved = [i for i in wanted if i in set(ready_ids)]
        if not resolved:
            raise ValidationFailed("None of the selected documents is ready to be searched.")
        return resolved

    # ---- memory -----------------------------------------------------------------------------

    async def memory_context(self, principal: Principal, conversation_id: UUID) -> MemoryContext:
        """The conversation's memory for the next turn. Ownership is checked in PostgreSQL first;
        Redis is never touched for a conversation the caller does not own."""
        if self._memory is None:
            raise RuntimeError("conversation memory is not configured")
        scope = principal.scope()
        async with self._uow_factory() as uow:
            conversation = await self._owned(uow, scope, conversation_id)
            state = ConversationState(
                conversation_id=conversation.id,
                last_completed_seq=conversation.last_completed_seq,
                summary=conversation.summary,
                summary_through_seq=conversation.summary_through_seq,
            )
        return await self._memory.load(scope, state)

    @staticmethod
    async def _owned(uow: UnitOfWork, scope: OwnershipScope, conversation_id: UUID) -> Conversation:
        conversation = await uow.conversations.get(scope, conversation_id)
        if conversation is None:
            raise NotFound("Conversation not found.")  # also for someone else's: no existence leak
        return conversation
