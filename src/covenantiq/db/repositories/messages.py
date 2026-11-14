from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update

from covenantiq.core.enums import GroundingStatus, MessageStatus, QueryIntent
from covenantiq.core.ids import uuid7
from covenantiq.db.models import Conversation, Message
from covenantiq.db.repositories.base import Repository, ensure_owned
from covenantiq.db.scope import OwnershipScope


def _owned_conversation_ids(scope: OwnershipScope) -> Any:
    return scope.apply(select(Conversation.id), Conversation)


class MessageRepository(Repository):
    async def add_pair(
        self,
        scope: OwnershipScope,
        conversation: Conversation,
        user_message: Message,
        assistant_message: Message,
    ) -> tuple[Message, Message]:
        """Allocate the next two sequence numbers and store a user message and its reply.

        The caller must hold the conversation row lock (ConversationRepository.get_for_update).
        """
        ensure_owned(scope, conversation)
        user_message.id = user_message.id or uuid7()
        user_message.conversation_id = conversation.id
        user_message.seq = conversation.last_message_seq + 1
        assistant_message.id = assistant_message.id or uuid7()
        assistant_message.conversation_id = conversation.id
        assistant_message.seq = conversation.last_message_seq + 2
        assistant_message.reply_to_id = user_message.id
        conversation.last_message_seq += 2
        self.session.add_all([user_message, assistant_message])
        await self.session.flush()
        return user_message, assistant_message

    async def get(self, scope: OwnershipScope, message_id: UUID) -> Message | None:
        statement = select(Message).where(
            Message.id == message_id, Message.conversation_id.in_(_owned_conversation_ids(scope))
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_by_client_id(
        self, scope: OwnershipScope, conversation_id: UUID, client_message_id: UUID
    ) -> Message | None:
        statement = select(Message).where(
            Message.conversation_id == conversation_id,
            Message.client_message_id == client_message_id,
            Message.conversation_id.in_(_owned_conversation_ids(scope)),
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_reply(self, scope: OwnershipScope, message_id: UUID) -> Message | None:
        """The assistant message that answers a user message."""
        statement = select(Message).where(
            Message.reply_to_id == message_id,
            Message.conversation_id.in_(_owned_conversation_ids(scope)),
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def recent(
        self,
        scope: OwnershipScope,
        conversation_id: UUID,
        limit: int,
        before_seq: int | None = None,
    ) -> Sequence[Message]:
        """The newest `limit` messages, returned oldest first."""
        statement = select(Message).where(
            Message.conversation_id == conversation_id,
            Message.conversation_id.in_(_owned_conversation_ids(scope)),
        )
        if before_seq is not None:
            statement = statement.where(Message.seq < before_seq)
        statement = statement.order_by(Message.seq.desc()).limit(limit)
        newest_first = (await self.session.execute(statement)).scalars().all()
        return list(reversed(newest_first))

    async def list(
        self, scope: OwnershipScope, conversation_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> Sequence[Message]:
        statement = (
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.conversation_id.in_(_owned_conversation_ids(scope)),
            )
            .order_by(Message.seq)
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.execute(statement)).scalars().all()

    async def complete(
        self,
        scope: OwnershipScope,
        message_id: UUID,
        *,
        content: str,
        status: MessageStatus,
        grounding_status: GroundingStatus | None = None,
        intent: QueryIntent | None = None,
        retrieval_trace: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> bool:
        """Finalize an assistant message. A completed message also advances the conversation's
        last_completed_seq, which the memory layer uses to detect a stale Redis window."""
        statement = (
            update(Message)
            .where(
                Message.id == message_id,
                Message.conversation_id.in_(_owned_conversation_ids(scope)),
            )
            .values(
                content=content,
                status=status,
                grounding_status=grounding_status,
                intent=intent,
                retrieval_trace=retrieval_trace,
                usage=usage,
                completed_at=func.now(),
            )
            .returning(Message.conversation_id, Message.seq)
            .execution_options(synchronize_session=False)
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return False
        if status is MessageStatus.COMPLETED:
            await self.session.execute(
                update(Conversation)
                .where(Conversation.id == row.conversation_id)
                .values(last_completed_seq=func.greatest(Conversation.last_completed_seq, row.seq))
                .execution_options(synchronize_session=False)
            )
        return True

    async def mark_streaming(self, scope: OwnershipScope, message_id: UUID) -> int | None:
        """pending -> streaming. Returns the message's seq, or None if it was not pending."""
        statement = (
            update(Message)
            .where(
                Message.id == message_id,
                Message.status == MessageStatus.PENDING,
                Message.conversation_id.in_(_owned_conversation_ids(scope)),
            )
            .values(status=MessageStatus.STREAMING)
            .returning(Message.seq)
            .execution_options(synchronize_session=False)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def mark_interrupted(self, scope: OwnershipScope, message_id: UUID) -> bool:
        """A reply whose producer stopped before it finished (shutdown). No effect on a message
        that already reached a final state."""
        statement = (
            update(Message)
            .where(
                Message.id == message_id,
                Message.status.in_([MessageStatus.PENDING, MessageStatus.STREAMING]),
                Message.conversation_id.in_(_owned_conversation_ids(scope)),
            )
            .values(status=MessageStatus.INTERRUPTED, completed_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return int((await self.session.execute(statement)).rowcount) > 0  # type: ignore[attr-defined]

    async def system_mark_interrupted_stale(self, older_than: datetime) -> int:
        """Startup sweep: messages left pending or streaming by a dead API process."""
        result = await self.session.execute(
            update(Message)
            .where(
                Message.status.in_([MessageStatus.PENDING, MessageStatus.STREAMING]),
                Message.created_at < older_than,
            )
            .values(status=MessageStatus.INTERRUPTED, completed_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount)  # type: ignore[attr-defined]
