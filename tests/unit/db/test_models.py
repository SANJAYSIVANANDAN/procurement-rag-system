from sqlalchemy.orm import configure_mappers

import covenantiq.db.models  # noqa: F401
from covenantiq.core.enums import DB_ENUMS
from covenantiq.db.base import Base, enum_type_name

EXPECTED_TABLES = {
    "users",
    "vendors",
    "documents",
    "document_clauses",
    "document_chunks",
    "document_processing_jobs",
    "conversations",
    "conversation_documents",
    "messages",
    "citations",
    "analysis_jobs",
    "contract_findings",
    "vendor_risk_assessments",
    "vendor_risk_dimensions",
}

# Tables whose rows are reached through a parent, so they carry no owner_id of their own.
NOT_DIRECTLY_OWNED = {
    "users",
    "conversation_documents",
    "messages",
    "citations",
    "vendor_risk_dimensions",
}


def test_mappers_configure() -> None:
    configure_mappers()


def test_all_spec_tables_exist() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_owned_tables_have_an_owner_column() -> None:
    for name, table in Base.metadata.tables.items():
        assert ("owner_id" in table.c) == (name not in NOT_DIRECTLY_OWNED), name


def test_enum_type_names_follow_the_spec() -> None:
    names = {enum_type_name(enum_cls) for enum_cls in DB_ENUMS}
    assert names == {
        "document_status",
        "contract_type",
        "job_status",
        "processing_stage",
        "message_role",
        "message_status",
        "query_intent",
        "grounding_status",
        "analysis_job_type",
        "risk_category",
        "clause_status",
        "severity",
        "evidence_sufficiency",
        "review_status",
        "risk_dimension",
        "dimension_status",
    }


def test_every_constraint_has_a_stable_name() -> None:
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            assert constraint.name is not None, f"unnamed constraint on {table.name}"
