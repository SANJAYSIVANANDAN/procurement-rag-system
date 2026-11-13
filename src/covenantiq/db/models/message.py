from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from covenantiq.core.enums import GroundingStatus, MessageRole, MessageStatus, QueryIntent
from covenantiq.core.ids import uuid7
from covenantiq.db.base import Base


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("conversation_id", "seq"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    seq: Mapped[int]
    role: Mapped[MessageRole]
    content: Mapped[str] = mapped_column(default="", server_default="")
    status: Mapped[MessageStatus] = mapped_column(default=MessageStatus.COMPLETED)
    client_message_id: Mapped[UUID | None]
    intent: Mapped[QueryIntent | None]
    grounding_status: Mapped[GroundingStatus | None]
    selected_document_ids: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    retrieval_trace: Mapped[dict[str, Any] | None]
    usage: Mapped[dict[str, Any] | None]
    reply_to_id: Mapped[UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None]


Index("ix_messages_conversation_seq_desc", Message.conversation_id, Message.seq.desc())
# Idempotent submission: the same client_message_id cannot be stored twice in a conversation.
Index(
    "uq_messages_conversation_client_message",
    Message.conversation_id,
    Message.client_message_id,
    unique=True,
    postgresql_where=text("client_message_id IS NOT NULL"),
)
