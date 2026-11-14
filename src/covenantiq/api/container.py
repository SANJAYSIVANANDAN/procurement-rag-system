from dataclasses import dataclass

from celery import Celery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from covenantiq.ai.llm.factory import build_llm
from covenantiq.analysis.contract.rules_loader import get_ruleset
from covenantiq.analysis.vendor.config_schema import get_vendor_config
from covenantiq.core.config import Settings
from covenantiq.core.security import PasswordHasher
from covenantiq.db.session import create_engine, create_session_factory
from covenantiq.db.uow import UnitOfWork
from covenantiq.graphs.deps import GraphDeps
from covenantiq.graphs.runner import GraphRunner
from covenantiq.ingestion.storage import FileStorage, LocalFileStorage
from covenantiq.memory.factory import build_memory_manager
from covenantiq.memory.manager import MemoryManager
from covenantiq.rag.citations import CitationValidator
from covenantiq.rag.config import load_rag_config
from covenantiq.rag.evidence import EvidenceAssessor
from covenantiq.rag.factory import build_retrieval_engine
from covenantiq.rag.retrieval import RetrievalEngine
from covenantiq.services.auth_service import AuthService
from covenantiq.services.chat_persistence import PostgresChatStore
from covenantiq.services.chat_service import ChatService
from covenantiq.services.contract_analysis_service import ContractAnalysisService
from covenantiq.services.conversation_service import ConversationService
from covenantiq.services.dlq_service import DlqService
from covenantiq.services.document_service import DocumentService
from covenantiq.services.job_service import JobService
from covenantiq.services.rate_limiter import RateLimiter
from covenantiq.services.reembed_service import ReembedService
from covenantiq.services.sweep_service import SweepService
from covenantiq.services.task_dispatcher import (
    CeleryTaskDispatcher,
    QueueDepthProbe,
    TaskDispatcher,
)
from covenantiq.services.vendor_assessment_service import VendorAssessmentService
from covenantiq.services.vendor_comparison_service import VendorComparisonService
from covenantiq.services.vendor_service import VendorService
from covenantiq.streaming.locks import ConversationLock
from covenantiq.streaming.publisher import NullPublisher
from covenantiq.workers.app_factory import create_celery_app
from covenantiq.workers.queues import DOCUMENT_PROCESSING

REDIS_TIMEOUT_SECONDS = 2


@dataclass
class Container:
    """Everything the API process shares: built once at startup, read by dependencies."""

    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    rate_limiter: RateLimiter
    auth_service: AuthService
    storage: FileStorage
    dispatcher: TaskDispatcher
    document_service: DocumentService
    conversation_service: ConversationService
    vendor_service: VendorService
    job_service: JobService
    sweep_service: SweepService
    celery_app: Celery
    dlq_service: DlqService
    reembed_service: ReembedService
    chat_service: ChatService
    contract_analysis_service: ContractAnalysisService
    vendor_assessment_service: VendorAssessmentService
    vendor_comparison_service: VendorComparisonService
    memory: MemoryManager
    retrieval: RetrievalEngine

    async def aclose(self) -> None:
        await self.chat_service.aclose()  # replies in flight finish (or are marked interrupted)
        await self.memory.aclose()
        await self.retrieval.aclose()
        await self.redis.aclose()
        self.celery_app.close()
        await self.engine.dispose()


def build_container(settings: Settings) -> Container:
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    uow_factory = lambda: UnitOfWork(session_factory)  # noqa: E731
    storage = LocalFileStorage(settings.upload_dir)
    celery_app = create_celery_app(settings, publisher_only=True)
    dispatcher = CeleryTaskDispatcher(celery_app)
    job_service = JobService(uow_factory)
    backlog = QueueDepthProbe(celery_app, DOCUMENT_PROCESSING)
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_timeout=REDIS_TIMEOUT_SECONDS,
        socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
    )
    llm = build_llm(settings)
    memory = build_memory_manager(settings, uow_factory, redis, llm.provider)
    conversation_service = ConversationService(uow_factory, memory)
    retrieval = build_retrieval_engine(settings, uow_factory, redis=redis)
    rag = load_rag_config(settings.rag_config_file).with_settings(settings)
    runner = GraphRunner(
        GraphDeps(
            retrieval=retrieval,
            llm=llm.provider,
            routes=llm.routes,
            memory=memory,
            evidence=EvidenceAssessor(rag.evidence),
            citations=CitationValidator(rag.citations),
            chat_store=PostgresChatStore(uow_factory),
            rag=rag,
        ),
        NullPublisher(),  # the Redis Streams publisher arrives with the SSE endpoint
    )
    chat_service = ChatService(
        conversation_service,
        runner,
        ConversationLock(redis, env=settings.env.value),
        uow_factory,
    )
    contract_analysis = ContractAnalysisService(uow_factory, dispatcher, job_service, get_ruleset())
    vendor_config = get_vendor_config()
    return Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis,
        rate_limiter=RateLimiter(redis, env=settings.env.value, overrides=settings.rate_limits),
        auth_service=AuthService(
            uow_factory,
            PasswordHasher(),
            jwt_secret=settings.jwt_secret.get_secret_value(),
            jwt_ttl_minutes=settings.jwt_ttl_minutes,
        ),
        storage=storage,
        dispatcher=dispatcher,
        document_service=DocumentService(
            uow_factory,
            storage,
            dispatcher,
            max_upload_bytes=settings.max_upload_mb * 1024 * 1024,
            max_pages=settings.max_pages,
            max_active_jobs=settings.max_active_jobs_per_user,
            backlog=backlog.depth,
            max_backlog=settings.max_ingest_backlog,
        ),
        conversation_service=conversation_service,
        vendor_service=VendorService(uow_factory),
        job_service=job_service,
        sweep_service=SweepService(job_service, dispatcher, redis, env=settings.env.value),
        celery_app=celery_app,
        dlq_service=DlqService(celery_app.connection_for_write, job_service, dispatcher),
        reembed_service=ReembedService(uow_factory, dispatcher),
        chat_service=chat_service,
        contract_analysis_service=contract_analysis,
        vendor_assessment_service=VendorAssessmentService(
            uow_factory, dispatcher, job_service, contract_analysis, get_ruleset(), vendor_config
        ),
        vendor_comparison_service=VendorComparisonService(
            uow_factory, dispatcher, job_service, vendor_config
        ),
        memory=memory,
        retrieval=retrieval,
    )
