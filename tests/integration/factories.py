from uuid import UUID, uuid4

from covenantiq.core.enums import (
    AnalysisJobType,
    ClauseStatus,
    DimensionStatus,
    EvidenceSufficiency,
    MessageRole,
    MessageStatus,
    RiskCategory,
    RiskDimension,
    Severity,
)
from covenantiq.db.models import (
    AnalysisJob,
    Citation,
    ContractFinding,
    Conversation,
    Document,
    DocumentChunk,
    DocumentClause,
    DocumentProcessingJob,
    Message,
    User,
    Vendor,
    VendorRiskAssessment,
    VendorRiskDimension,
)


def make_user(email: str | None = None) -> User:
    return User(email=email or f"{uuid4().hex[:8]}@example.com", password_hash="hash")


def make_vendor(owner_id: UUID, name: str = "Acme Corp") -> Vendor:
    return Vendor(owner_id=owner_id, name=name, normalized_name=name.lower())


def make_document(owner_id: UUID, **overrides: object) -> Document:
    fields: dict[str, object] = {
        "owner_id": owner_id,
        "title": "Master Services Agreement",
        "original_filename": "msa.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 1000,
        "sha256": uuid4().hex + uuid4().hex,
        "storage_path": "/data/uploads/x",
    }
    fields.update(overrides)
    return Document(**fields)


def make_processing_job(owner_id: UUID, document_id: UUID) -> DocumentProcessingJob:
    return DocumentProcessingJob(owner_id=owner_id, document_id=document_id)


def make_conversation(owner_id: UUID, **overrides: object) -> Conversation:
    return Conversation(owner_id=owner_id, title="Chat", **overrides)  # type: ignore[arg-type]


def make_message_pair(
    client_message_id: UUID | None = None, content: str = "What is the notice period?"
) -> tuple[Message, Message]:
    user = Message(
        role=MessageRole.USER,
        content=content,
        status=MessageStatus.COMPLETED,
        client_message_id=client_message_id,
    )
    assistant = Message(role=MessageRole.ASSISTANT, content="", status=MessageStatus.PENDING)
    return user, assistant


def make_analysis_job(
    owner_id: UUID,
    *,
    document_id: UUID | None = None,
    vendor_id: UUID | None = None,
    job_type: AnalysisJobType = AnalysisJobType.CONTRACT_ANALYSIS,
    idempotency_key: str | None = None,
) -> AnalysisJob:
    return AnalysisJob(
        owner_id=owner_id,
        job_type=job_type,
        document_id=document_id,
        vendor_id=vendor_id,
        idempotency_key=idempotency_key or f"key-{uuid4()}",
    )


def make_finding(
    owner_id: UUID,
    job_id: UUID,
    document_id: UUID,
    rule_id: str = "liability_cap",
    ordinal: int = 0,
) -> ContractFinding:
    return ContractFinding(
        owner_id=owner_id,
        analysis_job_id=job_id,
        document_id=document_id,
        rule_id=rule_id,
        ordinal=ordinal,
        risk_category=RiskCategory.LIABILITY,
        clause_status=ClauseStatus.PROBLEMATIC,
        severity=Severity.HIGH,
        title="Uncapped liability",
        explanation="No cap on liability.",
        evidence_sufficiency=EvidenceSufficiency.STRONG,
    )


def make_assessment(
    owner_id: UUID, vendor_id: UUID, job_id: UUID
) -> tuple[VendorRiskAssessment, list[VendorRiskDimension]]:
    assessment = VendorRiskAssessment(
        owner_id=owner_id,
        vendor_id=vendor_id,
        analysis_job_id=job_id,
        overall_severity=Severity.MEDIUM,
        scoring_config_version="v1",
    )
    dimensions = [
        VendorRiskDimension(
            dimension=RiskDimension.LEGAL,
            severity=Severity.MEDIUM,
            status=DimensionStatus.ASSESSED,
            score=42.0,
        ),
        VendorRiskDimension(
            dimension=RiskDimension.DEPENDENCY,
            severity=Severity.INFO,
            status=DimensionStatus.NOT_ASSESSED,
        ),
    ]
    return assessment, dimensions


def make_citation(document_id: UUID, **parents: UUID) -> Citation:
    return Citation(
        document_id=document_id,
        quote="Liability is unlimited.",
        document_title="Master Services Agreement",
        page_start=3,
        **parents,
    )


def make_clause(
    owner_id: UUID, document_id: UUID, ordinal: int, text: str = "Clause text.", **overrides: object
) -> DocumentClause:
    return DocumentClause(
        owner_id=owner_id,
        document_id=document_id,
        ordinal=ordinal,
        page_start=1,
        page_end=1,
        text=text,
        **overrides,  # type: ignore[arg-type]
    )


def make_chunk(
    owner_id: UUID, document_id: UUID, clause_id: UUID, ordinal: int, text: str = "Chunk text."
) -> DocumentChunk:
    return DocumentChunk(
        owner_id=owner_id,
        document_id=document_id,
        clause_id=clause_id,
        ordinal=ordinal,
        text=text,
        token_count=len(text.split()),
        page_start=1,
        page_end=1,
    )
