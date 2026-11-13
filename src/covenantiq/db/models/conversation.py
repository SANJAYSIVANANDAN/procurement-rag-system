from uuid import UUID

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from covenantiq.core.ids import uuid7
from covenantiq.db.base import Base, OwnedMixin, TimestampMixin


class Conversation(Base, TimestampMixin, OwnedMixin):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    vendor_id: Mapped[UUID | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"))
    title: Mapped[str | None]
    last_message_seq: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    last_completed_seq: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    summary: Mapped[str | None]
    summary_through_seq: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    archived: Mapped[bool] = mapped_column(default=False, server_default=text("false"))


Index("ix_conversations_owner_updated", Conversation.owner_id, Conversation.updated_at.desc())


class ConversationDocument(Base):
    __tablename__ = "conversation_documents"

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
