"""`ChatPersistencePort` on PostgreSQL: each call is its own unit of work (spec 22.2).

The chat graph never sees a session. It asks this to mark a reply as streaming, to read what the
memory layer needs, and to store the final message with its citations in one transaction.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID

from covenantiq.core.enums import DocumentStatus, MessageStatus
from covenantiq.core.errors import NotFound
from covenantiq.db.models import Citation
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.graphs.deps import ChatOutcome, StoredTurn
from covenantiq.memory.base import ConversationState
from covenantiq.services.conversation_service import DEFAULT_SCOPE_LIMIT

ACTIVE = (MessageStatus.PENDING, MessageStatus.STREAMING)


class PostgresChatStore:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def mark_streaming(self, scope: OwnershipScope, message_id: UUID) -> int:
        async with self._uow_factory() as uow:
            seq = await uow.messages.mark_streaming(scope, message_id)
            if seq is None:  # already streaming (a retry) or not there at all
                message = await uow.messages.get(scope, message_id)
                if message is None:
                    raise NotFound("Message not found.")
                seq = message.seq
            await uow.commit()
        return seq

    async def conversation_state(
        self, scope: OwnershipScope, conversation_id: UUID
    ) -> ConversationState:
        async with self._uow_factory() as uow:
            conversation = await uow.conversations.get(scope, conversation_id)
            if conversation is None:
                raise NotFound("Conversation not found.")
            return ConversationState(
                conversation_id=conversation.id,
                last_completed_seq=conversation.last_completed_seq,
                summary=conversation.summary,
                summary_through_seq=conversation.summary_through_seq,
            )

    async def document_titles(
        self, scope: OwnershipScope, document_ids: Sequence[UUID]
    ) -> dict[UUID, str]:
        wanted = set(document_ids)
        async with self._uow_factory() as uow:
            ready = await uow.documents.list(
                scope, status=DocumentStatus.READY, limit=DEFAULT_SCOPE_LIMIT
            )
            return {d.id: d.title for d in ready if d.id in wanted}

    async def complete(
        self, scope: OwnershipScope, message_id: UUID, outcome: ChatOutcome
    ) -> StoredTurn:
        async with self._uow_factory() as uow:
            message = await uow.messages.get(scope, message_id)
            if message is None:
                raise NotFound("Message not found.")
            user = (
                await uow.messages.get(scope, message.reply_to_id)
                if message.reply_to_id is not None
                else None
            )
            if user is None:
                raise NotFound("The question this reply answers was not found.")
            stored = StoredTurn(
                user_seq=user.seq,
                user_content=user.content,
                user_at=user.created_at,
                assistant_seq=message.seq,
                assistant_at=datetime.now(UTC),
            )
            if message.status not in ACTIVE:
                return stored  # already final: a late failure must not overwrite a finished reply
            await uow.messages.complete(
                scope,
                message_id,
                content=outcome.content,
                status=outcome.status,
                grounding_status=outcome.grounding_status,
                intent=outcome.intent,
                retrieval_trace=outcome.trace or None,
                usage=outcome.usage or None,
            )
            await uow.citations.bulk_add(
                scope,
                [
                    Citation(
                        message_id=message_id,
                        document_id=c.document_id,
                        chunk_id=c.chunk_id,
                        clause_id=c.clause_id,
                        marker=c.marker,
                        quote=c.quote,
                        document_title=c.document_title,
                        page_start=c.page_start,
                        page_end=c.page_end,
                        clause_ref=c.clause_ref,
                        validated=True,
                    )
                    for c in outcome.citations
                ],
            )
            await uow.commit()
        return stored

    async def mark_interrupted(self, scope: OwnershipScope, message_id: UUID) -> None:
        async with self._uow_factory() as uow:
            await uow.messages.mark_interrupted(scope, message_id)
            await uow.commit()
