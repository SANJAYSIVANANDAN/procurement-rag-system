"""The per-process runtime of a Celery prefork child (spec 25.7).

Each child owns one long-lived event loop and one container built on it. Tasks are synchronous
wrappers that hand their coroutine to `run_async`.
"""

import asyncio
import os
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from covenantiq.ai.embeddings.base import EmbeddingProvider
from covenantiq.ai.embeddings.factory import build_embedding_provider
from covenantiq.core.config import Settings, get_settings
from covenantiq.db.session import create_engine, create_session_factory
from covenantiq.db.uow import UnitOfWork
from covenantiq.ingestion.extraction.base import DocumentExtractor
from covenantiq.ingestion.extraction.docling_extractor import DoclingExtractor
from covenantiq.ingestion.storage import FileStorage, LocalFileStorage
from covenantiq.services.ingestion_service import ChunkingConfig, IngestionService
from covenantiq.services.job_service import JobService
from covenantiq.services.task_dispatcher import CeleryTaskDispatcher, TaskDispatcher
from covenantiq.workers.app_factory import create_celery_app


@dataclass
class WorkerContainer:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    storage: FileStorage
    extractor: DocumentExtractor
    job_service: JobService
    ingestion_service: IngestionService
    embedder: EmbeddingProvider

    async def aclose(self) -> None:
        await self.embedder.aclose()
        await self.engine.dispose()


def build_worker_container(
    settings: Settings,
    *,
    extractor: DocumentExtractor | None = None,
    embedder: EmbeddingProvider | None = None,
    dispatcher: TaskDispatcher | None = None,
) -> WorkerContainer:
    # NullPool: a pooled connection would be bound to one event loop, and tests and forks create
    # several.
    engine = create_engine(settings, null_pool=True)
    session_factory = create_session_factory(engine)

    def uow_factory() -> UnitOfWork:
        return UnitOfWork(session_factory)

    storage = LocalFileStorage(settings.upload_dir)
    job_service = JobService(uow_factory)
    # Models are baked into the image and found through DOCLING_ARTIFACTS_PATH; workers
    # never download at runtime.
    extractor = extractor or DoclingExtractor(
        page_batch=settings.docling_page_batch,
        artifacts_path=os.environ.get("DOCLING_ARTIFACTS_PATH") or None,
    )
    embedder = embedder or build_embedding_provider(settings)
    # Publishing the embedding task needs a broker connection only when a document is chunked.
    dispatcher = dispatcher or CeleryTaskDispatcher(
        create_celery_app(settings, publisher_only=True)
    )
    return WorkerContainer(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        storage=storage,
        extractor=extractor,
        job_service=job_service,
        ingestion_service=IngestionService(
            uow_factory,
            storage,
            job_service,
            extractor,
            embedder=embedder,
            dispatcher=dispatcher,
            chunking=ChunkingConfig(
                target_tokens=settings.chunk_target_tokens,
                parent_max_tokens=settings.parent_max_tokens,
                heading_depth=settings.clause_heading_depth,
            ),
            embed_batch_size=settings.embedding_batch_size,
        ),
        embedder=embedder,
    )


_loop: asyncio.AbstractEventLoop | None = None
_container: WorkerContainer | None = None


def init_runtime(settings: Settings | None = None) -> None:
    """Called once in each worker child (`worker_process_init`)."""
    install_runtime(build_worker_container(settings or get_settings()), asyncio.new_event_loop())


def install_runtime(container: WorkerContainer, loop: asyncio.AbstractEventLoop) -> None:
    global _loop, _container
    _loop, _container = loop, container


def shutdown_runtime() -> None:
    global _loop, _container
    if _loop is not None and _container is not None:
        with suppress(Exception):
            _loop.run_until_complete(_container.aclose())
        _loop.close()
    _loop = _container = None


def container() -> WorkerContainer:
    if _container is None:
        raise RuntimeError("the worker runtime has not been initialised")
    return _container


def run_async[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine to completion on this process's loop.

    If the call is interrupted (a soft time limit raises inside the loop), the coroutine is
    cancelled and unwound first, so no half-finished work is left to run during the next task.
    """
    if _loop is None:
        coroutine.close()
        raise RuntimeError("the worker runtime has not been initialised")
    task = _loop.create_task(coroutine)
    try:
        return _loop.run_until_complete(task)
    except BaseException:
        if not task.done():
            task.cancel()
            with suppress(BaseException):
                _loop.run_until_complete(task)
        raise
