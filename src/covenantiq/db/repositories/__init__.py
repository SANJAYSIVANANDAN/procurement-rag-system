from covenantiq.db.repositories.analysis_jobs import AnalysisJobRepository
from covenantiq.db.repositories.assessments import VendorAssessmentRepository
from covenantiq.db.repositories.base import Repository
from covenantiq.db.repositories.chunk_search import ChunkSearchRepository
from covenantiq.db.repositories.chunks import ChunkRepository
from covenantiq.db.repositories.citations import CitationRepository
from covenantiq.db.repositories.clauses import ClauseRepository
from covenantiq.db.repositories.conversations import ConversationRepository
from covenantiq.db.repositories.documents import DocumentRepository
from covenantiq.db.repositories.findings import FindingRepository
from covenantiq.db.repositories.messages import MessageRepository
from covenantiq.db.repositories.processing_jobs import ProcessingJobRepository
from covenantiq.db.repositories.users import UserRepository
from covenantiq.db.repositories.vendors import VendorRepository

ALL_REPOSITORIES: tuple[type[Repository], ...] = (
    AnalysisJobRepository,
    ChunkRepository,
    ChunkSearchRepository,
    CitationRepository,
    ClauseRepository,
    ConversationRepository,
    DocumentRepository,
    FindingRepository,
    MessageRepository,
    ProcessingJobRepository,
    UserRepository,
    VendorAssessmentRepository,
    VendorRepository,
)

__all__ = [
    "ALL_REPOSITORIES",
    "AnalysisJobRepository",
    "ChunkRepository",
    "ChunkSearchRepository",
    "CitationRepository",
    "ClauseRepository",
    "ConversationRepository",
    "DocumentRepository",
    "FindingRepository",
    "MessageRepository",
    "ProcessingJobRepository",
    "Repository",
    "UserRepository",
    "VendorAssessmentRepository",
    "VendorRepository",
]
