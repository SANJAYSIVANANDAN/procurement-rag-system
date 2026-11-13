"""users and vendors

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13 10:01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "vendors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("industry", sa.Text(), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_vendors_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendors")),
        sa.UniqueConstraint("owner_id", "normalized_name", name=op.f("uq_vendors_owner_id")),
    )
    op.create_index(op.f("ix_vendors_owner_id"), "vendors", ["owner_id"])
    op.create_index(
        "ix_vendors_normalized_name_trgm",
        "vendors",
        ["normalized_name"],
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_table("vendors")
    op.drop_table("users")
