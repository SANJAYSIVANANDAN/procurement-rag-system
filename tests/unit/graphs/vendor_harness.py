"""The vendor assessment graph on in-memory fakes: no database, no network."""

import re
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver

from covenantiq.ai.llm.fake import FakeLLMProvider
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.ai.schemas import (
    DimensionExplanation,
    SignalExtraction,
    SignalExtractionBatch,
)
from covenantiq.analysis.contract.rules_loader import get_ruleset
from covenantiq.analysis.vendor.config_schema import get_vendor_config
from covenantiq.analysis.vendor.types import EvidenceRef
from covenantiq.core.enums import ClauseStatus, Severity
from covenantiq.graphs.vendor_assessment.deps import (
    AssessmentRecord,
    DocumentAnalysis,
    FindingFact,
    VendorDeps,
    VendorDocument,
    VendorScope,
)
from covenantiq.graphs.vendor_assessment.runner import AssessmentOutcome, VendorAssessmentRunner
from covenantiq.rag.citations import CitationValidator
from covenantiq.rag.config import RagConfig
from tests.unit.graphs.analysis_harness import TEXT, ScriptedRetriever

CONFIG = get_vendor_config()
RULESET = get_ruleset()
DOC_A, DOC_B = UUID(int=21), UUID(int=22)
VENDOR_ID = UUID(int=7)


def evidence(doc: UUID = DOC_A, title: str = "Acme MSA") -> EvidenceRef:
    return EvidenceRef(
        document_id=doc, document_title=title, quote=TEXT, page_start=2, clause_ref="6.2"
    )


def findings(
    status: ClauseStatus, *, overrides: dict[str, ClauseStatus] | None = None, doc: UUID = DOC_A
) -> list[FindingFact]:
    """A finding for every contract rule: the same status, apart from the overrides."""
    overrides = overrides or {}
    return [
        FindingFact(
            rule_id=rule.id,
            status=overrides.get(rule.id, status),
            severity=Severity.HIGH
            if overrides.get(rule.id, status) is ClauseStatus.PROBLEMATIC
            else Severity.INFO,
            evidence=[evidence(doc)]
            if overrides.get(rule.id, status)
            in (ClauseStatus.ACCEPTABLE, ClauseStatus.PROBLEMATIC, ClauseStatus.AMBIGUOUS)
            else [],
        )
        for rule in RULESET.file.rules
    ]


class FakeVendorStore:
    def __init__(self, scope: VendorScope | None, analyses: list[DocumentAnalysis]) -> None:
        self.scope = scope
        self.analyses = analyses
        self.records: dict[UUID, AssessmentRecord] = {}
        self.persist_calls = 0
        self.progress: list[tuple[str, float]] = []

    async def load_scope(self, owner_id: UUID, vendor_id: UUID) -> VendorScope | None:
        return self.scope

    async def document_analyses(
        self, owner_id: UUID, document_ids: Sequence[UUID], ruleset_version: str
    ) -> list[DocumentAnalysis]:
        return self.analyses

    async def persist_assessment(
        self, owner_id: UUID, job_id: UUID, record: AssessmentRecord
    ) -> UUID:
        self.persist_calls += 1
        self.records[job_id] = record  # replaced, never appended
        return uuid4()

    async def report(self, job_id: UUID, stage: str, fraction: float) -> None:
        self.progress.append((stage, fraction))


def signals_in(prompt: str) -> list[str]:
    return re.findall(r'<signal id="([A-Z]+-[A-Z0-9-]+)"', prompt)


def first_source_of(prompt: str, signal_id: str) -> str:
    block = prompt.split(f'<signal id="{signal_id}"', 1)[1].split("</signal>", 1)[0]
    match = re.search(r"<source [^>]*>\n(.*?)\n</source>", block, re.S)
    return match.group(1) if match else ""


def favorable(prompt: str, signal_id: str) -> SignalExtraction:
    text = first_source_of(prompt, signal_id)
    return SignalExtraction(
        signal_id=signal_id,
        status="favorable" if text else "absent",
        supporting_markers=[1] if text else [],
        rationale=text or "Nothing was found.",
    )


@dataclass
class Vendor:
    scope: VendorScope | None = None
    analyses: list[DocumentAnalysis] | None = None
    llm: FakeLLMProvider = field(default_factory=FakeLLMProvider)
    saver: InMemorySaver = field(default_factory=InMemorySaver)
    job_id: UUID = field(default_factory=uuid4)
    owner_id: UUID = field(default_factory=uuid4)
    vendor_id: UUID = VENDOR_ID
    extract: Callable[[str, str], SignalExtraction] = favorable
    extraction_calls: int = 0
    on_extraction: Callable[[list[str]], None] | None = None

    def __post_init__(self) -> None:
        if self.scope is None:
            self.scope = VendorScope(
                vendor_id=self.vendor_id,
                name="Acme",
                documents=[VendorDocument(id=DOC_A, title="Acme MSA")],
            )
        if self.analyses is None:
            self.analyses = [
                DocumentAnalysis(
                    document_id=d.id,
                    state="current",
                    job_id=uuid4(),
                    findings=findings(ClauseStatus.ACCEPTABLE, doc=d.id),
                )
                for d in self.scope.documents
            ]
        self.store = FakeVendorStore(self.scope, self.analyses)
        self.retriever = ScriptedRetriever(DOC_A)
        self.llm.script("signal_extraction", self._extract)
        self.llm.script("explain", self._explain)
        rag = RagConfig()
        self.deps = VendorDeps(
            retrieval=self.retriever,
            llm=self.llm,
            routes=RouteTable({}, "fake-model"),
            store=self.store,
            config=CONFIG,
            ruleset_version=RULESET.version,
            rag=rag,
            citations=CitationValidator(rag.citations),
            scope_for=lambda owner: owner,
        )

    def _extract(self, request: Any) -> Any:
        prompt = request.messages[0].content
        ids = signals_in(prompt)
        self.extraction_calls += 1
        if self.on_extraction is not None:
            self.on_extraction(ids)
        return SignalExtractionBatch(extractions=[self.extract(prompt, i) for i in ids])

    @staticmethod
    def _explain(request: Any) -> Any:
        name = re.search(r'<dimension name="([a-z_]+)"', request.messages[0].content)
        label = name.group(1).replace("_", " ") if name else "this"
        return DimensionExplanation(
            reasoning=f"The {label} dimension reads as described.", mitigations=[]
        )

    def runner(self, **kw: Any) -> VendorAssessmentRunner:
        saver = self.saver

        @asynccontextmanager
        async def factory():  # type: ignore[no-untyped-def]
            yield saver

        return VendorAssessmentRunner(self.deps, self.store, factory, progress_interval=0.0, **kw)

    async def run(self, **kw: Any) -> AssessmentOutcome:
        return await self.runner(**kw).run(
            job_id=self.job_id, owner_id=self.owner_id, vendor_id=self.vendor_id
        )

    @property
    def record(self) -> AssessmentRecord:
        return self.store.records[self.job_id]

    def dimension(self, name: str):  # type: ignore[no-untyped-def]
        return next(d for d in self.record.score.dimensions if d.dimension.value == name)

    def signal(self, signal_id: str):  # type: ignore[no-untyped-def]
        return next(
            s for d in self.record.score.dimensions for s in d.signals if s.signal_id == signal_id
        )


def two_documents(vendor_id: UUID = VENDOR_ID) -> VendorScope:
    return VendorScope(
        vendor_id=vendor_id,
        name="Acme",
        documents=[
            VendorDocument(id=DOC_A, title="Acme MSA"),
            VendorDocument(id=DOC_B, title="Acme DPA"),
        ],
    )
