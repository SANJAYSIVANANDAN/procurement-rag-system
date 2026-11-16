"""remove the vendor comparison feature

Drops the vendor_comparisons table and any jobs of that type, and removes vendor_comparison from
the analysis_job_type enum. Postgres cannot drop one value from an enum in place, so the type is
recreated and the column is cast across.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16 10:06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_VALUES = ("contract_analysis", "vendor_assessment", "vendor_comparison")
NEW_VALUES = ("contract_analysis", "vendor_assessment")

OLD_CHECK = (
    "(job_type = 'contract_analysis' AND document_id IS NOT NULL) "
    "OR (job_type = 'vendor_assessment' AND vendor_id IS NOT NULL) "
    "OR job_type = 'vendor_comparison'"
)
NEW_CHECK = (
    "(job_type = 'contract_analysis' AND document_id IS NOT NULL) "
    "OR (job_type = 'vendor_assessment' AND vendor_id IS NOT NULL)"
)


def _recreate_enum(values: tuple[str, ...]) -> None:
    op.execute("ALTER TYPE analysis_job_type RENAME TO analysis_job_type_old")
    postgresql.ENUM(*values, name="analysis_job_type").create(op.get_bind())
    op.execute(
        "ALTER TABLE analysis_jobs ALTER COLUMN job_type TYPE analysis_job_type "
        "USING job_type::text::analysis_job_type"
    )
    op.execute("DROP TYPE analysis_job_type_old")


def upgrade() -> None:
    op.drop_table("vendor_comparisons")
    # A job of the removed type would block the enum cast below.
    op.execute("DELETE FROM analysis_jobs WHERE job_type = 'vendor_comparison'")

    # The old constraint's literals are compiled against the old enum type's OID. Dropping it
    # before the rename/recreate avoids "operator does not exist" when Postgres re-validates it.
    op.drop_constraint(op.f("ck_analysis_jobs_target_matches_type"), "analysis_jobs", type_="check")

    _recreate_enum(NEW_VALUES)

    op.create_check_constraint(
        op.f("ck_analysis_jobs_target_matches_type"), "analysis_jobs", NEW_CHECK
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_analysis_jobs_target_matches_type"), "analysis_jobs", type_="check")

    _recreate_enum(OLD_VALUES)

    op.create_check_constraint(
        op.f("ck_analysis_jobs_target_matches_type"), "analysis_jobs", OLD_CHECK
    )

    op.create_table(
        "vendor_comparisons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_job_id", sa.Uuid(), nullable=False),
        sa.Column("vendor_ids", postgresql.JSONB(), nullable=False),
        sa.Column("assessment_ids", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_job_id"],
            ["analysis_jobs.id"],
            name=op.f("fk_vendor_comparisons_analysis_job_id_analysis_jobs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_vendor_comparisons_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_comparisons")),
    )
    op.create_index(op.f("ix_vendor_comparisons_owner_id"), "vendor_comparisons", ["owner_id"])
