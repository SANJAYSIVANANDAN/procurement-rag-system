"""documents, clauses, chunks and processing jobs

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13 10:02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TSV_EXPRESSION = (
    "setweight(to_tsvector('english', coalesce(metadata->>'heading_text', '')), 'A') || "
    "setweight(to_tsvector('english', text), 'B')"
)


def _enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


document_status = _enum(
    "document_status",
    "uploaded",
    "queued",
    "extracting",
    "chunking",
    "embedding",
    "ready",
    "failed",
    "deleted",
)
contract_type = _enum(
    "contract_type",
    "msa",
    "saas",
    "dpa",
    "sow",
    "nda",
    "license",
    "supply",
    "services",
    "other",
    "unknown",
)
job_status = _enum("job_status", "queued", "running", "succeeded", "failed", "cancelled")
processing_stage = _enum(
    "processing_stage", "validating", "extracting", "chunking", "embedding", "finalizing"
)
ENUMS = (document_status, contract_type, job_status, processing_stage)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("status", document_status, nullable=False),
        sa.Column("contract_type", contract_type, nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "extraction_quality",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_documents_size_positive")),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_documents_owner_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_documents_vendor_id_vendors"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index(op.f("ix_documents_owner_id"), "documents", ["owner_id"])
    op.create_index(
        "ix_documents_owner_created", "documents", ["owner_id", sa.text("created_at DESC")]
    )
    op.create_index("ix_documents_owner_vendor", "documents", ["owner_id", "vendor_id"])
    op.create_index("ix_documents_owner_status", "documents", ["owner_id", "status"])
    op.create_index(
        "uq_documents_owner_sha256_active",
        "documents",
        ["owner_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "document_clauses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("clause_ref", sa.Text(), nullable=True),
        sa.Column("heading", sa.Text(), nullable=True),
        sa.Column(
            "heading_path",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "clause_types",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("has_table", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_clauses_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_document_clauses_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_clauses")),
        sa.UniqueConstraint("document_id", "ordinal", name=op.f("uq_document_clauses_document_id")),
    )
    op.create_index(op.f("ix_document_clauses_document_id"), "document_clauses", ["document_id"])
    op.create_index(op.f("ix_document_clauses_owner_id"), "document_clauses", ["owner_id"])
    op.create_index(
        "ix_document_clauses_clause_types",
        "document_clauses",
        ["clause_types"],
        postgresql_using="gin",
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("clause_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column(
            "clause_types",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(TSV_EXPRESSION, persisted=True),
            nullable=False,
        ),
        sa.Column("injection_score", sa.REAL(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["clause_id"],
            ["document_clauses.id"],
            name=op.f("fk_document_chunks_clause_id_document_clauses"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_document_chunks_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint("document_id", "ordinal", name=op.f("uq_document_chunks_document_id")),
    )
    op.create_index(op.f("ix_document_chunks_clause_id"), "document_chunks", ["clause_id"])
    op.create_index(op.f("ix_document_chunks_owner_id"), "document_chunks", ["owner_id"])
    op.create_index(
        "ix_document_chunks_owner_document", "document_chunks", ["owner_id", "document_id"]
    )
    op.create_index("ix_document_chunks_tsv", "document_chunks", ["tsv"], postgresql_using="gin")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    op.create_table(
        "document_processing_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("status", job_status, nullable=False),
        sa.Column("stage", processing_stage, nullable=True),
        sa.Column("progress", sa.REAL(), server_default=sa.text("0"), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("celery_task_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_processing_jobs_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_document_processing_jobs_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_processing_jobs")),
    )
    op.create_index(
        op.f("ix_document_processing_jobs_document_id"), "document_processing_jobs", ["document_id"]
    )
    op.create_index(
        op.f("ix_document_processing_jobs_owner_id"), "document_processing_jobs", ["owner_id"]
    )
    op.create_index(
        "uq_document_processing_jobs_active_document",
        "document_processing_jobs",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_table("document_processing_jobs")
    op.drop_table("document_chunks")
    op.drop_table("document_clauses")
    op.drop_table("documents")
    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind, checkfirst=True)
