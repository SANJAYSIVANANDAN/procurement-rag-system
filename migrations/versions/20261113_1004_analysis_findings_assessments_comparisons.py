"""analysis jobs, findings, assessments, comparisons and citations

Citations live here rather than with messages because they reference findings and risk
dimensions, which are created in this migration.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13 10:04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


analysis_job_type = _enum(
    "analysis_job_type", "contract_analysis", "vendor_assessment", "vendor_comparison"
)
risk_category = _enum(
    "risk_category",
    "liability",
    "indemnification",
    "termination",
    "payment",
    "renewal",
    "intellectual_property",
    "data_protection",
    "security",
    "sla",
    "confidentiality",
    "compliance",
    "dispute_resolution",
    "assignment",
    "insurance",
    "warranty",
    "obligations",
    "general",
)
clause_status = _enum(
    "clause_status", "absent", "problematic", "ambiguous", "insufficient_evidence", "acceptable"
)
severity = _enum("severity", "info", "low", "medium", "high", "critical")
evidence_sufficiency = _enum("evidence_sufficiency", "strong", "moderate", "weak", "none")
review_status = _enum("review_status", "unreviewed")
risk_dimension = _enum(
    "risk_dimension",
    "legal",
    "data_security",
    "operational",
    "financial_commercial",
    "compliance",
    "dependency",
)
dimension_status = _enum("dimension_status", "assessed", "insufficient_evidence", "not_assessed")
job_status = _enum("job_status", "queued", "running", "succeeded", "failed", "cancelled")
ENUMS = (
    analysis_job_type,
    risk_category,
    clause_status,
    severity,
    evidence_sufficiency,
    review_status,
    risk_dimension,
    dimension_status,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "analysis_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_type", analysis_job_type, nullable=False),
        sa.Column("status", job_status, nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("vendor_id", sa.Uuid(), nullable=True),
        sa.Column(
            "params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("ruleset_version", sa.Text(), nullable=True),
        sa.Column("scoring_config_version", sa.Text(), nullable=True),
        sa.Column("celery_task_id", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("progress", sa.REAL(), server_default=sa.text("0"), nullable=False),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(job_type = 'contract_analysis' AND document_id IS NOT NULL) "
            "OR (job_type = 'vendor_assessment' AND vendor_id IS NOT NULL) "
            "OR job_type = 'vendor_comparison'",
            name=op.f("ck_analysis_jobs_target_matches_type"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_analysis_jobs_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_analysis_jobs_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_analysis_jobs_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_jobs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_analysis_jobs_idempotency_key")),
    )
    op.create_index(op.f("ix_analysis_jobs_owner_id"), "analysis_jobs", ["owner_id"])
    op.create_index(
        "ix_analysis_jobs_owner_type_created",
        "analysis_jobs",
        ["owner_id", "job_type", sa.text("created_at DESC")],
    )

    op.create_table(
        "contract_findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_job_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("risk_category", risk_category, nullable=False),
        sa.Column("clause_status", clause_status, nullable=False),
        sa.Column("severity", severity, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("business_impact", sa.Text(), nullable=True),
        sa.Column("recommended_action", sa.Text(), nullable=True),
        sa.Column("clause_ref", sa.Text(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.REAL(), nullable=True),
        sa.Column("evidence_sufficiency", evidence_sufficiency, nullable=False),
        sa.Column("review_status", review_status, server_default="unreviewed", nullable=False),
        sa.Column("related_document_ids", postgresql.JSONB(), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "analyzed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_job_id"],
            ["analysis_jobs.id"],
            name=op.f("fk_contract_findings_analysis_job_id_analysis_jobs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_contract_findings_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_contract_findings_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contract_findings")),
        sa.UniqueConstraint(
            "analysis_job_id",
            "rule_id",
            "ordinal",
            name=op.f("uq_contract_findings_analysis_job_id"),
        ),
    )
    op.create_index(op.f("ix_contract_findings_owner_id"), "contract_findings", ["owner_id"])
    op.create_index(
        "ix_contract_findings_document_job", "contract_findings", ["document_id", "analysis_job_id"]
    )
    op.create_index(
        "ix_contract_findings_owner_severity", "contract_findings", ["owner_id", "severity"]
    )

    op.create_table(
        "vendor_risk_assessments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("vendor_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_job_id", sa.Uuid(), nullable=False),
        sa.Column("overall_score", sa.REAL(), nullable=True),
        sa.Column("overall_severity", severity, nullable=False),
        sa.Column("evidence_coverage", sa.REAL(), server_default=sa.text("0"), nullable=False),
        sa.Column("completeness", sa.REAL(), server_default=sa.text("0"), nullable=False),
        sa.Column("provisional", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_latest", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "weights", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("scoring_config_version", sa.Text(), nullable=False),
        sa.Column(
            "documents_considered",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "assessed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_job_id"],
            ["analysis_jobs.id"],
            name=op.f("fk_vendor_risk_assessments_analysis_job_id_analysis_jobs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_vendor_risk_assessments_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vendor_id"],
            ["vendors.id"],
            name=op.f("fk_vendor_risk_assessments_vendor_id_vendors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_risk_assessments")),
    )
    op.create_index(
        op.f("ix_vendor_risk_assessments_owner_id"), "vendor_risk_assessments", ["owner_id"]
    )
    op.create_index(
        op.f("ix_vendor_risk_assessments_vendor_id"), "vendor_risk_assessments", ["vendor_id"]
    )
    op.create_index(
        "uq_vendor_risk_assessments_latest",
        "vendor_risk_assessments",
        ["vendor_id"],
        unique=True,
        postgresql_where=sa.text("is_latest"),
    )

    op.create_table(
        "vendor_risk_dimensions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("dimension", risk_dimension, nullable=False),
        sa.Column("score", sa.REAL(), nullable=True),
        sa.Column("severity", severity, nullable=False),
        sa.Column("coverage", sa.REAL(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", dimension_status, nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column(
            "signals", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column(
            "missing_information",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "mitigations", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["vendor_risk_assessments.id"],
            name=op.f("fk_vendor_risk_dimensions_assessment_id_vendor_risk_assessments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vendor_risk_dimensions")),
        sa.UniqueConstraint(
            "assessment_id", "dimension", name=op.f("uq_vendor_risk_dimensions_assessment_id")
        ),
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

    op.create_table(
        "citations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("finding_id", sa.Uuid(), nullable=True),
        sa.Column("risk_dimension_id", sa.Uuid(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("clause_id", sa.Uuid(), nullable=True),
        sa.Column("marker", sa.Integer(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("document_title", sa.Text(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("clause_ref", sa.Text(), nullable=True),
        sa.Column("relevance_score", sa.REAL(), nullable=True),
        sa.Column("validated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "num_nonnulls(message_id, finding_id, risk_dimension_id) = 1",
            name=op.f("ck_citations_one_parent"),
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["document_chunks.id"],
            name=op.f("fk_citations_chunk_id_document_chunks"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["clause_id"],
            ["document_clauses.id"],
            name=op.f("fk_citations_clause_id_document_clauses"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_citations_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["contract_findings.id"],
            name=op.f("fk_citations_finding_id_contract_findings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_citations_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["risk_dimension_id"],
            ["vendor_risk_dimensions.id"],
            name=op.f("fk_citations_risk_dimension_id_vendor_risk_dimensions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_citations")),
    )
    op.create_index(op.f("ix_citations_message_id"), "citations", ["message_id"])
    op.create_index(op.f("ix_citations_finding_id"), "citations", ["finding_id"])
    op.create_index(op.f("ix_citations_risk_dimension_id"), "citations", ["risk_dimension_id"])


def downgrade() -> None:
    op.drop_table("citations")
    op.drop_table("vendor_comparisons")
    op.drop_table("vendor_risk_dimensions")
    op.drop_table("vendor_risk_assessments")
    op.drop_table("contract_findings")
    op.drop_table("analysis_jobs")
    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind, checkfirst=True)
