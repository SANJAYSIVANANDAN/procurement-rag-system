from collections.abc import Collection, Sequence
from uuid import UUID

from sqlalchemy import delete, select, update

from covenantiq.db.models import Conversation, ConversationDocument, Document
from covenantiq.db.repositories.base import Repository, ensure_owned
from covenantiq.db.scope import OwnershipScope


class ConversationRepository(Repository):
    async def create(self, scope: OwnershipScope, conversation: Conversation) -> Conversation:
        ensure_owned(scope, conversation)
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def get(self, scope: OwnershipScope, conversation_id: UUID) -> Conversation | None:
        statement = scope.apply(
            select(Conversation).where(Conversation.id == conversation_id), Conversation
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_for_update(
        self, scope: OwnershipScope, conversation_id: UUID
    ) -> Conversation | None:
        """Row-locks the conversation until the transaction ends (serializes message submission)."""
        statement = scope.apply(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True),
            Conversation,
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def list(
        self,
        scope: OwnershipScope,
        *,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Conversation]:
        statement = scope.apply(select(Conversation), Conversation)
        if not include_archived:
            statement = statement.where(Conversation.archived.is_(False))
        statement = (
            statement.order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.execute(statement)).scalars().all()

    async def update_summary(
        self, scope: OwnershipScope, conversation_id: UUID, summary: str, through_seq: int
    ) -> bool:
        statement = scope.apply(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(summary=summary, summary_through_seq=through_seq)
            .returning(Conversation.id)
            .execution_options(synchronize_session=False),
            Conversation,
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def set_documents(
        self, scope: OwnershipScope, conversation_id: UUID, document_ids: Collection[UUID]
    ) -> Sequence[UUID]:
        """Replace the conversation's document scope. Documents the caller does not own, or that
        are deleted, are silently dropped; the ids actually stored are returned."""
        if await self.get(scope, conversation_id) is None:
            return []
        await self.session.execute(
            delete(ConversationDocument).where(
                ConversationDocument.conversation_id == conversation_id
            )
        )
        allowed_query = scope.apply(
            select(Document.id).where(
                Document.id.in_(list(document_ids)), Document.deleted_at.is_(None)
            ),
            Document,
        )
        allowed = (await self.session.execute(allowed_query)).scalars().all()
        self.session.add_all(
            ConversationDocument(conversation_id=conversation_id, document_id=document_id)
            for document_id in allowed
        )
        await self.session.flush()
        return allowed

    async def document_ids(self, scope: OwnershipScope, conversation_id: UUID) -> Sequence[UUID]:
        statement = (
            select(ConversationDocument.document_id)
            .join(Conversation, Conversation.id == ConversationDocument.conversation_id)
            .where(Conversation.id == conversation_id)
        )
        statement = scope.apply(statement, Conversation)
        return (await self.session.execute(statement)).scalars().all()
