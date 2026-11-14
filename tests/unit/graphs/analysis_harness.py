"""The contract analysis graph on in-memory fakes: no database, no network."""

import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver

from covenantiq.ai.llm.fake import FakeLLMProvider
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.ai.schemas import (
    ObligationExtraction,
    RuleAssessment,
    RuleAssessmentBatch,
)
from covenantiq.analysis.contract.rules_loader import load_ruleset
from covenantiq.analysis.contract.types import FindingDraft
from covenantiq.graphs.contract_analysis.deps import AnalysisDeps, ChunkText, DocumentInfo
from covenantiq.graphs.contract_analysis.runner import AnalysisOutcome, ContractAnalysisRunner
from covenantiq.rag.citations import CitationValidator
from covenantiq.rag.config import AnalysisConfig, RagConfig
from covenantiq.rag.types import RetrievalRequest, RetrievalResult
from tests.unit.rag.builders import chunk, parent, result

RULESET = load_ruleset(Path("config/rules/contract_baseline_v1.yaml"))
TEXT = (
    "The Supplier's total liability under this Agreement shall not exceed the fees paid in the "
    "twelve months before the claim."
)


class Crash(BaseException):
    """A worker dying mid-run: not an `Exception`, so nothing in the graph can catch it."""


class FakeStore:
    def __init__(self, doc: DocumentInfo) -> None:
        self.doc = doc
        self.chunks: list[ChunkText] = []
        self.persisted: dict[UUID, list[FindingDraft]] = {}
        self.stats: dict[UUID, dict[str, Any]] = {}
        self.persist_calls = 0
        self.progress: list[tuple[str, float]] = []

    async def load_document(self, owner_id: UUID, document_id: UUID) -> DocumentInfo | None:
        return self.doc

    async def document_chunks(self, owner_id: UUID, document_id: UUID) -> list[ChunkText]:
        return self.chunks

    async def persist(self, owner_id, job_id, document_id, findings, stats) -> None:  # type: ignore[no-untyped-def]
        self.persist_calls += 1
        self.persisted[job_id] = list(findings)  # replaced, never appended: idempotent
        self.stats[job_id] = stats

    async def report(self, job_id: UUID, stage: str, fraction: float) -> None:
        self.progress.append((stage, fraction))


class ScriptedRetriever:
    """Returns the same good passage for every query, or nothing where `empty_for` matches."""

    def __init__(self, document_id: UUID) -> None:
        self.document_id = document_id
        self.empty_for: list[str] = []
        self.text_for: dict[str, str] = {}
        self.fail_for: list[str] = []
        self.requests: list[RetrievalRequest] = []

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.requests.append(request)
        query = request.query.lower()
        if any(m in query for m in self.fail_for):
            raise ConnectionError("pgvector is down")
        if any(m in query for m in self.empty_for):
            return result([])
        text = next((t for m, t in self.text_for.items() if m in query), TEXT)
        best = chunk(
            text, rerank=0.85, dense=1, lexical=1, document=self.document_id, title="Acme MSA"
        )
        return result([best], parents=[parent(best, text)])


def rules_in(prompt: str) -> list[str]:
    return re.findall(r'<rule id="([A-Z]+-\d{3})"', prompt)


def first_source(prompt: str, rule_id: str) -> str:
    block = prompt.split(f'<rule id="{rule_id}"', 1)[1].split("</rule>", 1)[0]
    match = re.search(r"<source [^>]*>\n(.*?)\n</source>", block, re.S)
    return match.group(1) if match else ""


def default_assessment(prompt: str, rule_id: str) -> RuleAssessment:
    text = first_source(prompt, rule_id)
    return RuleAssessment(
        rule_id=rule_id,
        clause_found="yes" if text else "no",
        supporting_markers=[1] if text else [],
        status="acceptable" if text else "absent",
        red_flags=[],
        extracted_values=[],
        explanation=text or "No passage was found.",
        business_impact="Limits exposure.",
        recommended_action="",
        confidence="high",
    )


@dataclass
class Analysis:
    doc: DocumentInfo
    llm: FakeLLMProvider = field(default_factory=FakeLLMProvider)
    saver: InMemorySaver = field(default_factory=InMemorySaver)
    job_id: UUID = field(default_factory=uuid4)
    owner_id: UUID = field(default_factory=uuid4)
    batches_assessed: list[tuple[str, ...]] = field(default_factory=list)
    assess: Callable[[str, str], RuleAssessment] = default_assessment
    on_batch: Callable[[list[str]], None] | None = None
    rules_per_batch: int = 4

    def __post_init__(self) -> None:
        self.store = FakeStore(self.doc)
        self.retriever = ScriptedRetriever(self.doc.id)
        self.llm.script("rule_assessment", self._batch)
        self.llm.script("signal_extraction", lambda r: ObligationExtraction(obligations=[]))
        rag = RagConfig(analysis=AnalysisConfig(rules_per_batch=self.rules_per_batch))
        self.deps = AnalysisDeps(
            retrieval=self.retriever,
            llm=self.llm,
            routes=RouteTable({}, "fake-model"),
            store=self.store,
            ruleset=RULESET,
            rag=rag,
            citations=CitationValidator(rag.citations),
            scope_for=lambda owner: owner,
        )

    def _batch(self, request: Any) -> Any:
        prompt = request.messages[0].content
        if "<value_a>" in prompt:  # a contradiction check, not a rule batch
            from covenantiq.ai.schemas import ContradictionVerdict

            return ContradictionVerdict(
                is_contradiction=True, explanation="The two clauses disagree."
            )
        ids = rules_in(prompt)
        if self.on_batch is not None:
            self.on_batch(ids)
        self.batches_assessed.append(tuple(ids))
        return RuleAssessmentBatch(assessments=[self.assess(prompt, i) for i in ids])

    def runner(self, **kw: Any) -> ContractAnalysisRunner:
        saver = self.saver

        @asynccontextmanager
        async def factory():  # type: ignore[no-untyped-def]
            yield saver

        return ContractAnalysisRunner(
            self.deps,
            self.store,
            factory,
            progress_interval=0.0,
            **kw,  # type: ignore[arg-type]
        )

    async def run(self, **kw: Any) -> AnalysisOutcome:
        return await self.runner(**kw).run(
            job_id=self.job_id, owner_id=self.owner_id, document_id=self.doc.id
        )

    @property
    def findings(self) -> list[FindingDraft]:
        return self.store.persisted[self.job_id]

    def by_rule(self, rule_id: str) -> FindingDraft:
        return next(f for f in self.findings if f.rule_id == rule_id)


def ready_document(**overrides: Any) -> DocumentInfo:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "title": "Acme MSA",
        "status": "ready",
        "contract_type": "saas",
        "extraction_quality": {
            "pages_total": 40,
            "pages_with_text": 40,
            "insufficient_text": False,
        },
        "pages_total": 40,
        "clause_types_present": [],
    }
    fields.update(overrides)
    return DocumentInfo(**fields)
