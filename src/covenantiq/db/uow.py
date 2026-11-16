from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from covenantiq.db.repositories import (
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


class UnitOfWork:
    """One database transaction with all repositories bound to it (spec 15.8).

    Nothing is persisted unless `commit()` is called; leaving the block on an exception, or
    without committing, discards the work.
    """

    session: AsyncSession
    users: UserRepository
    vendors: VendorRepository
    documents: DocumentRepository
    clauses: ClauseRepository
    chunks: ChunkRepository
    chunk_search: ChunkSearchRepository
    processing_jobs: ProcessingJobRepository
    conversations: ConversationRepository
    messages: MessageRepository
    citations: CitationRepository
    analysis_jobs: AnalysisJobRepository
    findings: FindingRepository
    assessments: VendorAssessmentRepository

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __aenter__(self) -> "UnitOfWork":
        self.session = self._session_factory()
        self.users = UserRepository(self.session)
        self.vendors = VendorRepository(self.session)
        self.documents = DocumentRepository(self.session)
        self.clauses = ClauseRepository(self.session)
        self.chunks = ChunkRepository(self.session)
        self.chunk_search = ChunkSearchRepository(self.session)
        self.processing_jobs = ProcessingJobRepository(self.session)
        self.conversations = ConversationRepository(self.session)
        self.messages = MessageRepository(self.session)
        self.citations = CitationRepository(self.session)
        self.analysis_jobs = AnalysisJobRepository(self.session)
        self.findings = FindingRepository(self.session)
        self.assessments = VendorAssessmentRepository(self.session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None:
                await self.session.rollback()
        finally:
            await self.session.close()

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
