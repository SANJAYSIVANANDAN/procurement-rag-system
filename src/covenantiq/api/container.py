from dataclasses import dataclass

from celery import Celery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from covenantiq.core.config import Settings
from covenantiq.core.security import PasswordHasher
from covenantiq.db.session import create_engine, create_session_factory
from covenantiq.db.uow import UnitOfWork
from covenantiq.ingestion.storage import FileStorage, LocalFileStorage
from covenantiq.services.auth_service import AuthService
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
from covenantiq.services.vendor_service import VendorService
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

    async def aclose(self) -> None:
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
        conversation_service=ConversationService(uow_factory),
        vendor_service=VendorService(uow_factory),
        job_service=job_service,
        sweep_service=SweepService(job_service, dispatcher, redis, env=settings.env.value),
        celery_app=celery_app,
        dlq_service=DlqService(celery_app.connection_for_write, job_service, dispatcher),
        reembed_service=ReembedService(uow_factory, dispatcher),
    )
