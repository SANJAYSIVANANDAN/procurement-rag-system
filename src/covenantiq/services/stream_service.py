"""Serving the events of a chat reply to its owner (spec 26.4 step 1).

The streaming package knows Redis and nothing else, so this is where a listener is checked against
PostgreSQL: the conversation must be the caller's, the message must belong to it, and it must be an
assistant message. Anything else is a 404, never a hint that the message exists.
"""

from collections.abc import AsyncIterator, Callable
from uuid import UUID

from covenantiq.core.enums import MessageRole
from covenantiq.core.errors import NotFound
from covenantiq.db.uow import UnitOfWork
from covenantiq.services.dto import Principal
from covenantiq.streaming.consumer import Disconnected, Frame, StoredReply, StreamConsumer


class StreamService:
    def __init__(self, consumer: StreamConsumer, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._consumer = consumer
        self._uow_factory = uow_factory

    async def _load(
        self, principal: Principal, conversation_id: UUID, message_id: UUID
    ) -> StoredReply:
        scope = principal.scope()
        async with self._uow_factory() as uow:
            message = await uow.messages.get(scope, message_id)
            if (
                message is None
                or message.conversation_id != conversation_id
                or message.role is not MessageRole.ASSISTANT
            ):
                raise NotFound("Message not found.")
            citations = await uow.citations.for_messages(scope, [message.id])
            return StoredReply(
                message_id=message.id,
                conversation_id=message.conversation_id,
                seq=message.seq,
                status=message.status,
                grounding_status=(
                    message.grounding_status.value if message.grounding_status else None
                ),
                content=message.content,
                citations=[
                    {
                        "marker": c.marker,
                        "document_id": str(c.document_id),
                        "document_title": c.document_title,
                        "page_start": c.page_start,
                        "page_end": c.page_end,
                        "clause_ref": c.clause_ref,
                        "quote": c.quote,
                        "validated": c.validated,
                    }
                    for c in citations
                ],
                usage={k: v for k, v in (message.usage or {}).items() if isinstance(v, int)},
            )

    async def open(
        self,
        principal: Principal,
        conversation_id: UUID,
        message_id: UUID,
        *,
        last_event_id: str | None = None,
        disconnected: Disconnected | None = None,
    ) -> AsyncIterator[Frame]:
        """Check the caller's right to the message (raising `NotFound` now, before any byte is
        sent), then return the frames."""
        await self._load(principal, conversation_id, message_id)

        async def load() -> StoredReply:
            return await self._load(principal, conversation_id, message_id)

        return self._consumer.frames(
            message_id, load=load, last_event_id=last_event_id, disconnected=disconnected
        )
