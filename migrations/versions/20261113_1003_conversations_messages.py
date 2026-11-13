"""conversations and messages

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-13 10:03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


message_role = _enum("message_role", "user", "assistant", "system")
message_status = _enum(
    "message_status", "pending", "streaming", "completed", "failed", "interrupted"
)
query_intent = _enum(
    "query_intent",
    "document_qa",
    "clause_lookup",
    "comparison_qa",
    "summary",
    "analysis_request",
    "smalltalk",
    "out_of_scope",
)
grounding_status = _enum(
    "grounding_status",
    "grounded",
    "partially_grounded",
    "abstained",
    "ungrounded",
    "not_applicable",
)
ENUMS = (message_role, message_status, query_intent, grounding_status)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("last_message_seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_completed_seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_through_seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_conversations_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_conversations_vendor_id_vendors"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(op.f("ix_conversations_owner_id"), "conversations", ["owner_id"])
    op.create_index(
        "ix_conversations_owner_updated", "conversations", ["owner_id", sa.text("updated_at DESC")]
    )

    op.create_table(
        "conversation_documents",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_documents_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_conversation_documents_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "conversation_id", "document_id", name=op.f("pk_conversation_documents")
        ),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", message_role, nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("status", message_status, nullable=False),
        sa.Column("client_message_id", sa.Uuid(), nullable=True),
        sa.Column("intent", query_intent, nullable=True),
        sa.Column("grounding_status", grounding_status, nullable=True),
        sa.Column(
            "selected_document_ids",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("retrieval_trace", postgresql.JSONB(), nullable=True),
        sa.Column("usage", postgresql.JSONB(), nullable=True),
        sa.Column("reply_to_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reply_to_id"],
            ["messages.id"],
            name=op.f("fk_messages_reply_to_id_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("conversation_id", "seq", name=op.f("uq_messages_conversation_id")),
    )
    op.create_index(
        "ix_messages_conversation_seq_desc", "messages", ["conversation_id", sa.text("seq DESC")]
    )
    op.create_index(
        "uq_messages_conversation_client_message",
        "messages",
        ["conversation_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("conversation_documents")
    op.drop_table("conversations")
    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind, checkfirst=True)
