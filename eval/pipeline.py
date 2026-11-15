"""Running the production pipeline over the evaluation dataset.

Everything here is the real code: the ingestion service (chunking, clause types, embedding), the
hybrid retrieval engine, the chat graph with its evidence gate and citation validator, the contract
analysis graph with its heuristics and status logic, and the vendor scoring engine. What differs
between runs is only which providers it is given (offline: hash embeddings, a lexical reranker and
a scripted model; full: the configured real ones).
"""

import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from covenantiq.ai.embeddings.base import EmbeddingProvider
from covenantiq.ai.llm.base import LLMProvider, LLMRequest, StructuredResult, T
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.ai.rerank.base import Reranker
from covenantiq.analysis.contract.rules_loader import get_ruleset
from covenantiq.analysis.contract.types import FindingDraft
from covenantiq.analysis.vendor.aggregation import aggregate_all
from covenantiq.analysis.vendor.config_schema import get_vendor_config
from covenantiq.analysis.vendor.scoring import score_assessment
from covenantiq.analysis.vendor.signals import FindingFacts, map_contract_signals
from covenantiq.analysis.vendor.types import EvidenceRef
from covenantiq.core.enums import ContractType, MessageStatus
from covenantiq.core.ids import uuid7
from covenantiq.db.models import Document, DocumentProcessingJob, User
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.graphs.contract_analysis.deps import AnalysisDeps
from covenantiq.graphs.contract_analysis.graph import build_contract_graph
from covenantiq.graphs.deps import ChatOutcome, GraphDeps, StoredTurn
from covenantiq.graphs.runner import GraphRunner
from covenantiq.ingestion.storage import LocalFileStorage
from covenantiq.memory.base import CompletedTurn, ConversationState, MemoryContext, SessionTurn
from covenantiq.rag.cache import NullCache
from covenantiq.rag.citations import CitationValidator
from covenantiq.rag.config import RagConfig
from covenantiq.rag.evidence import EvidenceAssessor
from covenantiq.rag.retrieval import RetrievalEngine
from covenantiq.rag.session import search_session_factory
from covenantiq.rag.types import RetrievalRequest, RetrievalResult
from covenantiq.services.analysis_store import PostgresAnalysisStore
from covenantiq.services.ingestion_service import IngestionService
from covenantiq.services.job_service import JobService
from covenantiq.services.task_dispatcher import NullTaskDispatcher
from covenantiq.streaming.publisher import InMemoryPublisher
from eval.dataset import ChatExample, Dataset, FindingsExample, VendorCase
from eval.extraction import TextExtractor
from eval.metrics.answers import EmittedCitation

UowFactory = Callable[[], UnitOfWork]


@dataclass
class World:
    """The evaluation database after ingestion: one user, every document chunked and embedded."""

    uow_factory: UowFactory
    owner_id: UUID
    documents: dict[str, UUID]  # file name -> document id
    retrieval: RetrievalEngine
    rag: RagConfig

    @property
    def scope(self) -> OwnershipScope:
        return OwnershipScope(self.owner_id)

    def ids(self, names: Sequence[str]) -> list[UUID]:
        return [self.documents[n] for n in names]


async def ingest(
    dataset: Dataset,
    uow_factory: UowFactory,
    embedder: EmbeddingProvider,
    reranker: Reranker | None,
    storage_root: Path,
    rag: RagConfig,
    *,
    only: set[str] | None = None,
    exact_scan_threshold: int = 5000,
) -> World:
    """Chunk and embed the dataset's documents (or just `only`) with the real ingestion service."""
    contract_types = {f.document: ContractType(f.contract_type) for f in dataset.findings}
    async with uow_factory() as uow:
        user = await uow.users.add(
            User(email=f"eval-{uuid.uuid4().hex[:10]}@example.com", password_hash="not-a-login")  # noqa: S106
        )
        await uow.commit()
    scope = OwnershipScope(user.id)
    storage = LocalFileStorage(storage_root)
    jobs = JobService(uow_factory)
    service = IngestionService(
        uow_factory,
        storage,
        jobs,
        TextExtractor(),
        embedder=embedder,
        dispatcher=NullTaskDispatcher(),
    )
    documents: dict[str, UUID] = {}
    for name in dataset.document_names:
        if only is not None and name not in only:
            continue
        source = dataset.document_path(name)
        data = source.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        document_id = uuid7()
        relative = f"{user.id}/{document_id}/{sha}.txt"
        target = storage_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        async with uow_factory() as uow:
            await uow.documents.add(
                scope,
                Document(
                    id=document_id,
                    owner_id=user.id,
                    title=name,
                    original_filename=name,
                    mime_type="text/plain",
                    size_bytes=len(data),
                    sha256=sha,
                    storage_path=relative,
                    contract_type=contract_types.get(name, ContractType.UNKNOWN),
                ),
            )
            job = await uow.processing_jobs.create(
                scope, DocumentProcessingJob(owner_id=user.id, document_id=document_id)
            )
            await uow.commit()
        chunked = await service.process(job.id, attempt=0)
        if chunked.get("status") != "chunked":
            raise RuntimeError(f"{name} was not chunked: {chunked}")
        embedded = await service.process(
            job.id, attempt=0
        )  # the same entry point; stage picks the half
        if embedded.get("status") != "ready":
            raise RuntimeError(f"{name} was not embedded: {embedded}")
        documents[name] = document_id
    engine = RetrievalEngine(
        search_session_factory(uow_factory),
        embedder,
        reranker,
        rag,
        cache=NullCache(),
        env="eval",
        exact_scan_threshold=exact_scan_threshold,
    )
    return World(uow_factory, user.id, documents, engine, rag)


async def cleanup(world: World) -> None:
    """Remove the evaluation user and, by cascade, everything the run created."""
    from sqlalchemy import delete

    async with world.uow_factory() as uow:
        await uow.session.execute(delete(User).where(User.id == world.owner_id))
        await uow.commit()


# ---- the model, counted ------------------------------------------------------------------------


class CountingLLM:
    """Wraps a provider and records which routes were called, so a run can tell whether the
    answer model was reached (it is not, when the evidence gate abstains first)."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.routes: list[str] = []
        self.model_name = inner.model_name

    async def stream(self, request: LLMRequest) -> Any:
        self.routes.append(request.route)
        async for event in self._inner.stream(request):
            yield event

    async def structured(self, request: LLMRequest, schema: type[T]) -> StructuredResult[T]:
        self.routes.append(request.route)
        return await self._inner.structured(request, schema)

    async def count_tokens(self, request: LLMRequest) -> int:
        return await self._inner.count_tokens(request)


class RecordingRetriever:
    """Remembers the last retrieval, so the passages the answer was built from can be measured."""

    def __init__(self, engine: RetrievalEngine) -> None:
        self._engine = engine
        self.last: RetrievalResult | None = None

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.last = await self._engine.retrieve(request)
        return self.last


# ---- chat ---------------------------------------------------------------------------------------


class EvalChatStore:
    """The chat graph's persistence port, in memory."""

    def __init__(self, world: World) -> None:
        self._world = world
        self.outcomes: dict[UUID, ChatOutcome] = {}

    async def mark_streaming(self, scope: object, message_id: UUID) -> int:
        return 2

    async def conversation_state(self, scope: object, conversation_id: UUID) -> ConversationState:
        return ConversationState(conversation_id, 0, None, 0)

    async def document_titles(self, scope: object, document_ids: Sequence[UUID]) -> dict[UUID, str]:
        names = {v: k for k, v in self._world.documents.items()}
        return {i: names[i] for i in document_ids if i in names}

    async def complete(self, scope: object, message_id: UUID, outcome: ChatOutcome) -> StoredTurn:
        self.outcomes[message_id] = outcome
        now = datetime.now(UTC)
        return StoredTurn(1, "", now, 2, now)

    async def mark_interrupted(self, scope: object, message_id: UUID) -> None:
        return None


class EvalMemory:
    """Conversation memory for one question: the history the example carries, nothing else."""

    def __init__(self) -> None:
        self.history: list[SessionTurn] = []

    def set_history(self, example: ChatExample) -> None:
        now = datetime.now(UTC)
        self.history = [
            SessionTurn(
                seq=i + 1, role=t.role, content=t.content, ts=now, tokens=len(t.content) // 4
            )
            for i, t in enumerate(example.conversation_history)
        ]

    async def load(
        self, scope: object, state: ConversationState, query: str | None = None
    ) -> MemoryContext:
        return MemoryContext(summary=None, recent_turns=list(self.history))

    async def record_turn(self, scope: object, conversation_id: UUID, turn: CompletedTurn) -> None:
        return None

    async def invalidate(self, scope: object, conversation_id: UUID) -> None:
        return None


@dataclass
class ChatRun:
    example_id: str
    content: str = ""
    grounding: str | None = None
    status: str = ""
    citations: list[EmittedCitation] = field(default_factory=list)
    retrieved: list[str] = field(default_factory=list)
    validation: dict[str, int] = field(default_factory=dict)
    answer_model_called: bool = False
    error: str | None = None

    @property
    def abstained(self) -> bool:
        return self.grounding == "abstained"


class ChatEvaluator:
    def __init__(self, world: World, llm: LLMProvider, routes: RouteTable) -> None:
        self._world = world
        self.llm = CountingLLM(llm)
        self._retriever = RecordingRetriever(world.retrieval)
        self._store = EvalChatStore(world)
        self._memory = EvalMemory()
        self._publisher = InMemoryPublisher()
        rag = world.rag
        self._runner = GraphRunner(
            GraphDeps(
                retrieval=self._retriever,
                llm=self.llm,
                routes=routes,
                memory=self._memory,
                evidence=EvidenceAssessor(rag.evidence),
                citations=CitationValidator(rag.citations),
                chat_store=self._store,
                rag=rag,
            ),
            self._publisher,
        )

    async def run(self, example: ChatExample) -> ChatRun:
        self._memory.set_history(example)
        message_id = uuid.uuid4()
        before = len(self.llm.routes)
        await self._runner.run_chat(
            scope=self._world.scope,
            conversation_id=uuid.uuid4(),
            user_message_id=uuid.uuid4(),
            assistant_message_id=message_id,
            question=example.question,
            document_ids=self._world.ids(example.document_refs),
        )
        called = self.llm.routes[before:]
        outcome = self._store.outcomes.get(message_id)
        run = ChatRun(example.id, answer_model_called="answer" in called)
        if outcome is None or outcome.status is not MessageStatus.COMPLETED:
            run.error = str((outcome.trace or {}).get("error")) if outcome else "no outcome"
            run.status = "failed"
            return run
        run.content = outcome.content
        run.status = outcome.status.value
        run.grounding = outcome.grounding_status.value if outcome.grounding_status else None
        run.validation = {k: int(v) for k, v in (outcome.trace.get("validation") or {}).items()}
        retrieved = self._retriever.last
        texts = {c.chunk_id: c.text for c in (retrieved.chunks if retrieved else [])}
        run.retrieved = [c.text for c in (retrieved.chunks if retrieved else [])]
        run.citations = [
            EmittedCitation(
                document=c.document_title,
                page_start=c.page_start,
                page_end=c.page_end,
                text=texts.get(c.chunk_id, c.quote) if c.chunk_id else c.quote,
            )
            for c in outcome.citations
        ]
        return run


# ---- contract findings --------------------------------------------------------------------------


class _CapturingStore(PostgresAnalysisStore):
    """Reads documents from the evaluation database, and keeps the findings instead of storing."""

    def __init__(self, uow_factory: UowFactory, jobs: JobService) -> None:
        super().__init__(uow_factory, jobs)
        self.captured: dict[UUID, list[FindingDraft]] = {}

    async def persist(
        self,
        owner_id: UUID,
        job_id: UUID,
        document_id: UUID,
        findings: Sequence[FindingDraft],
        stats: dict[str, Any],
    ) -> None:
        self.captured[job_id] = list(findings)


@dataclass
class FindingsRun:
    example_id: str
    document: str
    drafts: list[FindingDraft] = field(default_factory=list)
    error: str | None = None


class FindingsEvaluator:
    def __init__(self, world: World, llm: LLMProvider, routes: RouteTable) -> None:
        self._world = world
        jobs = JobService(world.uow_factory)
        self._store = _CapturingStore(world.uow_factory, jobs)
        rag = world.rag
        self._graph = build_contract_graph(
            AnalysisDeps(
                retrieval=world.retrieval,
                llm=llm,
                routes=routes,
                store=self._store,
                ruleset=get_ruleset(),
                rag=rag,
                citations=CitationValidator(rag.citations),
                scope_for=OwnershipScope,
            )
        )

    async def run(self, example: FindingsExample) -> FindingsRun:
        job_id = uuid.uuid4()
        run = FindingsRun(example.id, example.document)
        state = await self._graph.ainvoke(
            {
                "job_id": str(job_id),
                "owner_id": str(self._world.owner_id),
                "document_id": str(self._world.documents[example.document]),
            },
            {"max_concurrency": 3},
        )
        if state.get("error"):
            run.error = str(state["error"].get("code"))
            return run
        run.drafts = self._store.captured.get(job_id, [])
        return run


# ---- vendors ------------------------------------------------------------------------------------


@dataclass
class VendorRun:
    case_id: str
    vendor: str
    signals: dict[str, str] = field(default_factory=dict)
    dimension_scores: dict[str, float | None] = field(default_factory=dict)
    deterministic: bool = True


def evaluate_vendor(case: VendorCase, findings: dict[str, FindingsRun], world: World) -> VendorRun:
    """Score a vendor from the findings of its documents with the pure engine, three times."""
    config = get_vendor_config()
    document_ids = world.ids(case.documents)
    facts: dict[UUID, list[FindingFacts]] = {}
    for name in case.documents:
        analysed = findings[name]
        facts[world.documents[name]] = [
            FindingFacts(
                d.rule_id,
                d.clause_status,
                d.severity,
                [
                    EvidenceRef(
                        document_id=e.document_id,
                        document_title=e.document_title,
                        quote=e.text[:200],
                    )
                    for e in d.evidence
                ],
            )
            for d in analysed.drafts
        ]

    def score() -> dict[str, Any]:
        observations = map_contract_signals(config.signals.signals, document_ids, facts)
        aggregates = aggregate_all(config.signals.signals, observations)
        return {
            "aggregates": {k: (v.status) for k, v in aggregates.items()},
            "score": score_assessment(config.scoring, config.signals, aggregates).model_dump(
                mode="json"
            ),
        }

    first = score()
    deterministic = all(score() == first for _ in range(2))
    result = VendorRun(case.id, case.vendor, deterministic=deterministic)
    result.signals = {k: v for k, v in first["aggregates"].items() if k in case.expected_signals}
    result.dimension_scores = {d["dimension"]: d["score"] for d in first["score"]["dimensions"]}
    return result
