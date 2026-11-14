"""page columns on clauses and chunks become nullable

DOCX has no pages, so a clause or chunk from one has no page to record.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-13 10:05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = [
    ("document_clauses", "page_start"),
    ("document_clauses", "page_end"),
    ("document_chunks", "page_start"),
    ("document_chunks", "page_end"),
]


def upgrade() -> None:
    for table, column in COLUMNS:
        op.alter_column(table, column, existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    for table, column in COLUMNS:
        op.execute(f"UPDATE {table} SET {column} = 0 WHERE {column} IS NULL")  # noqa: S608
        op.alter_column(table, column, existing_type=sa.Integer(), nullable=False)
