"""The per-process runtime of a Celery prefork child (spec 25.7).

Each child owns one long-lived event loop and one container built on it. Tasks are synchronous
wrappers that hand their coroutine to `run_async`.
"""

import asyncio
import os
import sys
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from covenantiq.ai.embeddings.base import EmbeddingProvider
from covenantiq.ai.embeddings.factory import build_embedding_provider
from covenantiq.ai.llm.factory import build_llm
from covenantiq.analysis.contract.rules_loader import get_ruleset
from covenantiq.analysis.vendor.config_schema import get_vendor_config
from covenantiq.core.config import Settings, get_settings
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.session import create_engine, create_session_factory
from covenantiq.db.uow import UnitOfWork
from covenantiq.graphs.checkpointed import postgres_checkpointer
from covenantiq.graphs.contract_analysis.deps import AnalysisDeps
from covenantiq.graphs.contract_analysis.runner import ContractAnalysisRunner
from covenantiq.graphs.vendor_assessment.deps import VendorDeps
from covenantiq.graphs.vendor_assessment.runner import VendorAssessmentRunner
from covenantiq.graphs.vendor_comparison.deps import ComparisonDeps
from covenantiq.graphs.vendor_comparison.runner import VendorComparisonRunner
from covenantiq.ingestion.extraction.base import DocumentExtractor
from covenantiq.ingestion.extraction.docling_extractor import DoclingExtractor
from covenantiq.ingestion.storage import FileStorage, LocalFileStorage
from covenantiq.rag.citations import CitationValidator
from covenantiq.rag.config import load_rag_config
from covenantiq.rag.factory import build_retrieval_engine
from covenantiq.rag.retrieval import RetrievalEngine
from covenantiq.services.analysis_store import PostgresAnalysisStore
from covenantiq.services.contract_analysis_service import ContractAnalysisService
from covenantiq.services.ingestion_service import ChunkingConfig, IngestionService
from covenantiq.services.job_service import JobService
from covenantiq.services.task_dispatcher import CeleryTaskDispatcher, TaskDispatcher
from covenantiq.services.vendor_assessment_service import VendorAssessmentService
from covenantiq.services.vendor_comparison_service import VendorComparisonService
from covenantiq.services.vendor_store import PostgresVendorStore
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
    contract_analysis_service: ContractAnalysisService
    vendor_assessment_service: VendorAssessmentService
    vendor_comparison_service: VendorComparisonService
    retrieval: RetrievalEngine

    async def aclose(self) -> None:
        await self.retrieval.aclose()
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
    llm = build_llm(settings)
    retrieval = build_retrieval_engine(settings, uow_factory)
    ruleset = get_ruleset()
    rag = load_rag_config(settings.rag_config_file).with_settings(settings)
    store = PostgresAnalysisStore(uow_factory, job_service)
    analysis_service = ContractAnalysisService(
        uow_factory,
        dispatcher,
        job_service,
        ruleset,
        ContractAnalysisRunner(
            AnalysisDeps(
                retrieval=retrieval,
                llm=llm.provider,
                routes=llm.routes,
                store=store,
                ruleset=ruleset,
                rag=rag,
                citations=CitationValidator(rag.citations),
                scope_for=OwnershipScope,
            ),
            store,
            postgres_checkpointer(settings.checkpoint_db_url.get_secret_value()),
            max_concurrency=settings.analysis_max_concurrency,
        ),
    )
    vendor_config = get_vendor_config()
    vendor_store = PostgresVendorStore(uow_factory, job_service, rag.analysis)
    checkpointer = postgres_checkpointer(settings.checkpoint_db_url.get_secret_value())
    assessment_service = VendorAssessmentService(
        uow_factory,
        dispatcher,
        job_service,
        analysis_service,
        ruleset,
        vendor_config,
        VendorAssessmentRunner(
            VendorDeps(
                retrieval=retrieval,
                llm=llm.provider,
                routes=llm.routes,
                store=vendor_store,
                config=vendor_config,
                ruleset_version=ruleset.version,
                rag=rag,
                citations=CitationValidator(rag.citations),
                scope_for=OwnershipScope,
            ),
            vendor_store,
            checkpointer,
            max_concurrency=settings.analysis_max_concurrency,
        ),
    )
    comparison_service = VendorComparisonService(
        uow_factory,
        dispatcher,
        job_service,
        vendor_config,
        VendorComparisonRunner(
            ComparisonDeps(
                llm=llm.provider, routes=llm.routes, store=vendor_store, config=vendor_config
            ),
            vendor_store,
            checkpointer,
        ),
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
        contract_analysis_service=analysis_service,
        vendor_assessment_service=assessment_service,
        vendor_comparison_service=comparison_service,
        retrieval=retrieval,
    )


def new_event_loop() -> asyncio.AbstractEventLoop:
    """psycopg (the LangGraph checkpointer) cannot use Windows' default proactor loop."""
    return asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()


_loop: asyncio.AbstractEventLoop | None = None
_container: WorkerContainer | None = None


def init_runtime(settings: Settings | None = None) -> None:
    """Called once in each worker child (`worker_process_init`)."""
    install_runtime(build_worker_container(settings or get_settings()), new_event_loop())


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
