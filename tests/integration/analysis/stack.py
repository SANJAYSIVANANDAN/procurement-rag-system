"""Contract analysis on real PostgreSQL (jobs, findings, citations, LangGraph checkpoints) with a
fake model and a retriever that returns a prepared passage from a real chunk."""

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from covenantiq.ai.llm.fake import FakeLLMProvider
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.ai.schemas import ObligationExtraction, RuleAssessmentBatch
from covenantiq.analysis.contract.rules_loader import load_ruleset
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.graphs.checkpointed import postgres_checkpointer
from covenantiq.graphs.checkpointing import psycopg_url, setup_checkpointer
from covenantiq.graphs.contract_analysis.deps import AnalysisDeps
from covenantiq.graphs.contract_analysis.runner import ContractAnalysisRunner
from covenantiq.rag.citations import CitationValidator
from covenantiq.rag.config import RagConfig
from covenantiq.services.analysis_store import PostgresAnalysisStore
from covenantiq.services.contract_analysis_service import ContractAnalysisService
from covenantiq.services.dto import Principal
from covenantiq.services.job_service import JobService
from tests.fakes import RecordingDispatcher
from tests.integration.chat.stack import GatedRetriever, retrieval_for, seed_document
from tests.integration.memory.helpers import new_user
from tests.unit.graphs.analysis_harness import default_assessment, rules_in

UowFactory = Callable[[], UnitOfWork]
RULESET = load_ruleset(Path("config/rules/contract_baseline_v1.yaml"))
CHECKPOINT_TABLES = (
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
    "checkpoint_migrations",
)


@dataclass
class Stack:
    service: ContractAnalysisService
    jobs: JobService
    llm: FakeLLMProvider
    dispatcher: RecordingDispatcher
    retriever: GatedRetriever
    store: PostgresAnalysisStore
    principal: Principal
    scope: OwnershipScope
    document_id: UUID
    uow_factory: UowFactory
    batch_calls: list[tuple[str, ...]]


@pytest.fixture
def clean_checkpoints(database_url: str) -> Iterator[None]:
    """The checkpoint tables are outside Alembic, so the migrations test must never meet them."""
    from tests.integration.test_checkpointer import _run

    asyncio.run(setup_checkpointer(psycopg_url(database_url)))
    yield
    asyncio.run(_run(database_url, f"DROP TABLE IF EXISTS {', '.join(CHECKPOINT_TABLES)} CASCADE"))


async def build_stack(uow_factory: UowFactory, database_url: str) -> Stack:
    scope = await new_user(uow_factory)
    principal = Principal(user_id=scope.owner_id, email="analyst@example.com")
    document_id, clause_id, chunk_id = await seed_document(uow_factory, scope)
    jobs = JobService(uow_factory)
    llm = FakeLLMProvider()
    batch_calls: list[tuple[str, ...]] = []

    def batch(request: Any) -> Any:
        prompt = request.messages[0].content
        ids = rules_in(prompt)
        batch_calls.append(tuple(ids))
        return RuleAssessmentBatch(assessments=[default_assessment(prompt, i) for i in ids])

    llm.script("rule_assessment", batch)
    llm.script("signal_extraction", lambda r: ObligationExtraction(obligations=[]))
    retriever = GatedRetriever(retrieval_for(document_id, scope.owner_id, clause_id, chunk_id))
    store = PostgresAnalysisStore(uow_factory, jobs)
    rag = RagConfig()
    runner = ContractAnalysisRunner(
        AnalysisDeps(
            retrieval=retriever,
            llm=llm,
            routes=RouteTable({}, "fake-model"),
            store=store,
            ruleset=RULESET,
            rag=rag,
            citations=CitationValidator(rag.citations),
            scope_for=OwnershipScope,
        ),
        store,
        postgres_checkpointer(database_url),
        progress_interval=0.0,
    )
    dispatcher = RecordingDispatcher()
    service = ContractAnalysisService(uow_factory, dispatcher, jobs, RULESET, runner)
    return Stack(
        service,
        jobs,
        llm,
        dispatcher,
        retriever,
        store,
        principal,
        scope,
        document_id,
        uow_factory,
        batch_calls,
    )
