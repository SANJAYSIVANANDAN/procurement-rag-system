"""Vendor assessment on real PostgreSQL: a vendor with documents, the contract analysis service
in front of it, and the vendor assessment service, with a fake model."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import update

from covenantiq.ai.llm.fake import FakeLLMProvider
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.ai.schemas import (
    DimensionExplanation,
    ObligationExtraction,
    RuleAssessmentBatch,
    SignalExtractionBatch,
)
from covenantiq.analysis.vendor.config_schema import get_vendor_config
from covenantiq.core.enums import ContractType, DocumentStatus
from covenantiq.db.models import Document
from covenantiq.db.scope import OwnershipScope
from covenantiq.graphs.checkpointed import postgres_checkpointer
from covenantiq.graphs.contract_analysis.deps import AnalysisDeps
from covenantiq.graphs.contract_analysis.runner import ContractAnalysisRunner
from covenantiq.graphs.vendor_assessment.deps import VendorDeps
from covenantiq.graphs.vendor_assessment.runner import VendorAssessmentRunner
from covenantiq.rag.citations import CitationValidator
from covenantiq.rag.config import RagConfig
from covenantiq.services.analysis_store import PostgresAnalysisStore
from covenantiq.services.contract_analysis_service import ContractAnalysisService
from covenantiq.services.dto import Principal
from covenantiq.services.job_service import JobService
from covenantiq.services.vendor_assessment_service import VendorAssessmentService
from covenantiq.services.vendor_store import PostgresVendorStore
from tests.fakes import RecordingDispatcher
from tests.integration.analysis.stack import RULESET, UowFactory
from tests.integration.chat.stack import GatedRetriever, retrieval_for, seed_document
from tests.integration.factories import make_vendor
from tests.integration.memory.helpers import new_user
from tests.unit.graphs.analysis_harness import default_assessment, rules_in
from tests.unit.graphs.vendor_harness import favorable, signals_in

VCONFIG = get_vendor_config()


@dataclass
class VendorStack:
    contract: ContractAnalysisService
    assessments: VendorAssessmentService
    jobs: JobService
    llm: FakeLLMProvider
    dispatcher: RecordingDispatcher
    retriever: GatedRetriever
    principal: Principal
    scope: OwnershipScope
    vendor_id: UUID
    document_ids: list[UUID]
    uow_factory: UowFactory
    extraction_calls: list[tuple[str, ...]]

    async def run_pending_contract_analyses(self) -> None:
        """What the analysis workers would do with the queued contract analysis tasks."""
        for _, job_id in list(self.dispatcher.analysis):
            async with self.uow_factory() as uow:
                job = await uow.analysis_jobs.system_get(job_id)
            if job is not None and job.job_type.value == "contract_analysis":
                await self.contract.run(job_id)

    async def assessment_job(self) -> UUID:
        return next(
            j for t, j in reversed(self.dispatcher.analysis) if t.value == "vendor_assessment"
        )

    async def new_vendor(self, name: str, documents: int = 1) -> tuple[UUID, list[UUID]]:
        """Another vendor of the same user, with its own ready documents."""
        return await seed_vendor(self.uow_factory, self.scope, name, documents, self.retriever)

    async def assess(self, vendor_id: UUID | None = None) -> UUID:
        """Start an assessment, run its contract analyses, then run it: the whole chain."""
        started = await self.assessments.start(self.principal, vendor_id or self.vendor_id)
        await self.run_pending_contract_analyses()
        result = await self.assessments.run(started.job.id)
        assert result["status"] == "succeeded", result
        return started.job.id


async def seed_vendor(
    uow_factory: UowFactory,
    scope: OwnershipScope,
    name: str,
    documents: int,
    retriever: GatedRetriever | None = None,
) -> tuple[UUID, list[UUID]]:
    async with uow_factory() as uow:
        vendor = await uow.vendors.add(scope, make_vendor(scope.owner_id, name))
        await uow.commit()
    ids: list[UUID] = []
    for index in range(documents):
        document_id, clause_id, chunk_id = await seed_document(
            uow_factory, scope, title=f"{name} contract {index + 1}"
        )
        async with uow_factory() as uow:
            await uow.session.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(vendor_id=vendor.id, contract_type=ContractType.SAAS)
            )
            await uow.commit()
        ids.append(document_id)
        if retriever is not None:  # any real chunk will do: citations only need rows that exist
            retriever.ready = retrieval_for(document_id, scope.owner_id, clause_id, chunk_id)
    return vendor.id, ids


async def set_status(stack: VendorStack, document_id: UUID, status: DocumentStatus) -> None:
    async with stack.uow_factory() as uow:
        await uow.documents.system_set_status(
            document_id, [DocumentStatus.READY, DocumentStatus.EMBEDDING], status
        )
        await uow.commit()


async def build_vendor_stack(
    uow_factory: UowFactory,
    database_url: str,
    *,
    documents: int = 2,
    scope: OwnershipScope | None = None,
) -> VendorStack:
    scope = scope or await new_user(uow_factory)
    principal = Principal(user_id=scope.owner_id, email="buyer@example.com")
    retriever = GatedRetriever(retrieval_for(UUID(int=1), scope.owner_id, UUID(int=2), UUID(int=3)))
    vendor_id, document_ids = await seed_vendor(uow_factory, scope, "Acme", documents, retriever)

    jobs = JobService(uow_factory)
    llm = FakeLLMProvider()
    calls: list[tuple[str, ...]] = []

    def rule_batch(request: Any) -> Any:
        prompt = request.messages[0].content
        return RuleAssessmentBatch(
            assessments=[default_assessment(prompt, i) for i in rules_in(prompt)]
        )

    def extraction(request: Any) -> Any:
        prompt = request.messages[0].content
        if "<vendor name=" not in prompt:  # the contract analysis's obligations pass
            return ObligationExtraction(obligations=[])
        ids = signals_in(prompt)
        calls.append(tuple(ids))
        return SignalExtractionBatch(extractions=[favorable(prompt, i) for i in ids])

    llm.script("rule_assessment", rule_batch)
    llm.script("signal_extraction", extraction)
    llm.script(
        "explain",
        lambda r: DimensionExplanation(reasoning="A summary of this dimension.", mitigations=[]),
    )

    rag = RagConfig()
    routes = RouteTable({}, "fake-model")
    analysis_store = PostgresAnalysisStore(uow_factory, jobs)
    vendor_store = PostgresVendorStore(uow_factory, jobs, rag.analysis)
    checkpointer = postgres_checkpointer(database_url)
    dispatcher = RecordingDispatcher()
    citations = CitationValidator(rag.citations)
    contract = ContractAnalysisService(
        uow_factory,
        dispatcher,
        jobs,
        RULESET,
        ContractAnalysisRunner(
            AnalysisDeps(
                retrieval=retriever,
                llm=llm,
                routes=routes,
                store=analysis_store,
                ruleset=RULESET,
                rag=rag,
                citations=citations,
                scope_for=OwnershipScope,
            ),
            analysis_store,
            checkpointer,
            progress_interval=0.0,
        ),
    )
    assessments = VendorAssessmentService(
        uow_factory,
        dispatcher,
        jobs,
        contract,
        RULESET,
        VCONFIG,
        VendorAssessmentRunner(
            VendorDeps(
                retrieval=retriever,
                llm=llm,
                routes=routes,
                store=vendor_store,
                config=VCONFIG,
                ruleset_version=RULESET.version,
                rag=rag,
                citations=citations,
                scope_for=OwnershipScope,
            ),
            vendor_store,
            checkpointer,
            progress_interval=0.0,
        ),
    )
    return VendorStack(
        contract,
        assessments,
        jobs,
        llm,
        dispatcher,
        retriever,
        principal,
        scope,
        vendor_id,
        document_ids,
        uow_factory,
        calls,
    )
