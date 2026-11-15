"""A chat graph wired to in-memory fakes: no database, no Redis, no network."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from covenantiq.ai.llm.base import ChatMessage
from covenantiq.ai.llm.fake import FakeLLMProvider
from covenantiq.ai.llm.retry import RetryingLLMProvider
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.core.enums import MessageStatus
from covenantiq.graphs.deps import ChatOutcome, GraphDeps, StoredTurn
from covenantiq.graphs.runner import GraphRunner
from covenantiq.memory.base import CompletedTurn, ConversationState, MemoryContext, SessionTurn
from covenantiq.rag.citations import CitationValidator
from covenantiq.rag.config import RagConfig
from covenantiq.rag.evidence import EvidenceAssessor
from covenantiq.rag.types import RetrievalRequest, RetrievalResult
from covenantiq.streaming.events import SSEEvent, check_order
from covenantiq.streaming.publisher import InMemoryPublisher
from tests.unit.rag.builders import chunk, parent, result

DOC = UUID(int=1000)
SOURCE_TEXT = (
    "The Supplier's total liability under this Agreement shall not exceed the fees paid in the "
    "twelve months before the claim."
)
GOOD_ANSWER = (
    "The Supplier's total liability is limited to the fees paid in the twelve months before the "
    "claim. [C1]"
)
QUESTION = "What is the total liability limit on fees?"


def good_retrieval() -> RetrievalResult:
    best = chunk(SOURCE_TEXT, rerank=0.9, dense=1, lexical=1, document=DOC)
    second = chunk("Liability for fees is capped.", rerank=0.6, dense=2, lexical=2, document=DOC)
    return result([best, second], parents=[parent(best, SOURCE_TEXT)])


def weak_retrieval() -> RetrievalResult:
    return result([chunk("Unrelated boilerplate about notices.", rerank=0.05, document=DOC)])


async def _no_sleep(seconds: float) -> None:
    return None


class FakeStore:
    def __init__(self) -> None:
        self.streaming: list[UUID] = []
        self.completed: dict[UUID, ChatOutcome] = {}
        self.interrupted: list[UUID] = []
        self.fail_complete = False
        self.titles = {DOC: "Acme MSA"}

    async def mark_streaming(self, scope: object, message_id: UUID) -> int:
        self.streaming.append(message_id)
        return 2

    async def conversation_state(self, scope: object, conversation_id: UUID) -> ConversationState:
        return ConversationState(conversation_id, 0, None, 0)

    async def document_titles(self, scope: object, document_ids: Sequence[UUID]) -> dict[UUID, str]:
        return {i: t for i, t in self.titles.items() if i in set(document_ids)}

    async def complete(self, scope: object, message_id: UUID, outcome: ChatOutcome) -> StoredTurn:
        if self.fail_complete:
            raise RuntimeError("database is down")
        self.completed[message_id] = outcome
        now = datetime.now(UTC)
        return StoredTurn(1, "the question", now, 2, now)

    async def mark_interrupted(self, scope: object, message_id: UUID) -> None:
        self.interrupted.append(message_id)


class FakeMemory:
    def __init__(self) -> None:
        self.context = MemoryContext(summary=None, recent_turns=[])
        self.recorded: list[CompletedTurn] = []
        self.invalidated = 0
        self.fail_load = False
        self.fail_record = False

    async def load(self, scope: object, state: ConversationState) -> MemoryContext:
        if self.fail_load:
            raise ConnectionError("redis is down")
        return self.context

    async def record_turn(self, scope: object, conversation_id: UUID, turn: CompletedTurn) -> None:
        if self.fail_record:
            raise ConnectionError("redis is down")
        self.recorded.append(turn)

    async def invalidate(self, scope: object, conversation_id: UUID) -> None:
        self.invalidated += 1


class FakeRetriever:
    def __init__(self) -> None:
        self.next: RetrievalResult | Exception = good_retrieval()
        self.requests: list[RetrievalRequest] = []

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.requests.append(request)
        if isinstance(self.next, Exception):
            raise self.next
        return self.next


@dataclass
class Harness:
    llm: FakeLLMProvider = field(default_factory=FakeLLMProvider)
    store: FakeStore = field(default_factory=FakeStore)
    memory: FakeMemory = field(default_factory=FakeMemory)
    retriever: FakeRetriever = field(default_factory=FakeRetriever)
    publisher: InMemoryPublisher = field(default_factory=InMemoryPublisher)
    conversation_id: UUID = field(default_factory=uuid4)
    message_id: UUID = field(default_factory=uuid4)
    retrying: bool = False  # put the provider's retry wrapper in front of the fake

    def __post_init__(self) -> None:
        self.llm.script("answer", lambda request: GOOD_ANSWER)
        self.llm.script("classify", lambda r: {"intent": "document_qa", "is_follow_up": False})
        rag = RagConfig()
        self.runner = GraphRunner(
            GraphDeps(
                retrieval=self.retriever,
                llm=RetryingLLMProvider(self.llm, sleep=_no_sleep) if self.retrying else self.llm,
                routes=RouteTable({}, "fake-model"),
                memory=self.memory,
                evidence=EvidenceAssessor(rag.evidence),
                citations=CitationValidator(rag.citations),
                chat_store=self.store,
                rag=rag,
            ),
            self.publisher,
        )

    async def ask(
        self, question: str = QUESTION, documents: Sequence[UUID] = (DOC,)
    ) -> list[SSEEvent]:
        await self.runner.run_chat(
            scope=object(),
            conversation_id=self.conversation_id,
            user_message_id=uuid4(),
            assistant_message_id=self.message_id,
            question=question,
            document_ids=documents,
        )
        events = self.publisher.of(self.message_id)
        check_order(events)  # every run, whatever happened, obeys the ordering guarantee
        return events

    @property
    def outcome(self) -> ChatOutcome:
        return self.store.completed[self.message_id]

    def routes_called(self) -> list[str]:
        return [request.route for request in self.llm.requests]

    def last_answer_request_messages(self) -> list[ChatMessage]:
        return [r for r in self.llm.requests if r.route == "answer"][-1].messages

    @staticmethod
    def completed_status() -> MessageStatus:
        return MessageStatus.COMPLETED


def turns(*pairs: tuple[str, str]) -> list[SessionTurn]:
    out: list[SessionTurn] = []
    for i, (q, a) in enumerate(pairs):
        for role, text in (("user", q), ("assistant", a)):
            out.append(
                SessionTurn(
                    seq=i * 2 + (1 if role == "user" else 2),
                    role=role,  # type: ignore[arg-type]
                    content=text,
                    ts=datetime.now(UTC),
                    tokens=10,
                )
            )
    return out
