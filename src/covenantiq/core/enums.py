from enum import StrEnum


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class LogFormat(StrEnum):
    CONSOLE = "console"
    JSON = "json"


# Database enumerations (spec 15.2). Each becomes a native PostgreSQL enum named after the
# snake-cased class name, and stores the lowercase value.
class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    EXTRACTING = "extracting"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class ContractType(StrEnum):
    MSA = "msa"
    SAAS = "saas"
    DPA = "dpa"
    SOW = "sow"
    NDA = "nda"
    LICENSE = "license"
    SUPPLY = "supply"
    SERVICES = "services"
    OTHER = "other"
    UNKNOWN = "unknown"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProcessingStage(StrEnum):
    VALIDATING = "validating"
    EXTRACTING = "extracting"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    FINALIZING = "finalizing"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageStatus(StrEnum):
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class QueryIntent(StrEnum):
    DOCUMENT_QA = "document_qa"
    CLAUSE_LOOKUP = "clause_lookup"
    COMPARISON_QA = "comparison_qa"
    SUMMARY = "summary"
    ANALYSIS_REQUEST = "analysis_request"
    SMALLTALK = "smalltalk"
    OUT_OF_SCOPE = "out_of_scope"


class GroundingStatus(StrEnum):
    GROUNDED = "grounded"
    PARTIALLY_GROUNDED = "partially_grounded"
    ABSTAINED = "abstained"
    UNGROUNDED = "ungrounded"
    NOT_APPLICABLE = "not_applicable"


class AnalysisJobType(StrEnum):
    CONTRACT_ANALYSIS = "contract_analysis"
    VENDOR_ASSESSMENT = "vendor_assessment"


class RiskCategory(StrEnum):
    LIABILITY = "liability"
    INDEMNIFICATION = "indemnification"
    TERMINATION = "termination"
    PAYMENT = "payment"
    RENEWAL = "renewal"
    INTELLECTUAL_PROPERTY = "intellectual_property"
    DATA_PROTECTION = "data_protection"
    SECURITY = "security"
    SLA = "sla"
    CONFIDENTIALITY = "confidentiality"
    COMPLIANCE = "compliance"
    DISPUTE_RESOLUTION = "dispute_resolution"
    ASSIGNMENT = "assignment"
    INSURANCE = "insurance"
    WARRANTY = "warranty"
    OBLIGATIONS = "obligations"
    GENERAL = "general"


class ClauseStatus(StrEnum):
    ABSENT = "absent"
    PROBLEMATIC = "problematic"
    AMBIGUOUS = "ambiguous"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ACCEPTABLE = "acceptable"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EvidenceSufficiency(StrEnum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    NONE = "none"


class ReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"


class RiskDimension(StrEnum):
    LEGAL = "legal"
    DATA_SECURITY = "data_security"
    OPERATIONAL = "operational"
    FINANCIAL_COMMERCIAL = "financial_commercial"
    COMPLIANCE = "compliance"
    DEPENDENCY = "dependency"


class DimensionStatus(StrEnum):
    ASSESSED = "assessed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NOT_ASSESSED = "not_assessed"


DB_ENUMS: tuple[type[StrEnum], ...] = (
    DocumentStatus,
    ContractType,
    JobStatus,
    ProcessingStage,
    MessageRole,
    MessageStatus,
    QueryIntent,
    GroundingStatus,
    AnalysisJobType,
    RiskCategory,
    ClauseStatus,
    Severity,
    EvidenceSufficiency,
    ReviewStatus,
    RiskDimension,
    DimensionStatus,
)
