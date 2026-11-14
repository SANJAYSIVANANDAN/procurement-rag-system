"""The chat service on real PostgreSQL and Redis, with a fake model and a scripted retriever."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select

from covenantiq.ai.llm.fake import FakeLLMProvider
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.core.enums import DocumentStatus
from covenantiq.db.models import Conversation, Message
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.graphs.deps import GraphDeps
from covenantiq.graphs.runner import GraphRunner
from covenantiq.memory import MemoryManager
from covenantiq.rag.citations import CitationValidator
from covenantiq.rag.config import RagConfig
from covenantiq.rag.evidence import EvidenceAssessor
from covenantiq.rag.types import RetrievalRequest, RetrievalResult
from covenantiq.services.chat_persistence import PostgresChatStore
from covenantiq.services.chat_service import ChatService
from covenantiq.services.conversation_service import ConversationService
from covenantiq.services.dto import Principal
from covenantiq.streaming.locks import ConversationLock
from covenantiq.streaming.publisher import InMemoryPublisher
from tests.integration.factories import make_chunk, make_clause, make_document
from tests.integration.memory.helpers import build, new_user
from tests.unit.graphs.harness import GOOD_ANSWER, SOURCE_TEXT, weak_retrieval
from tests.unit.rag.builders import chunk, parent, result

UowFactory = Callable[[], UnitOfWork]


class GatedRetriever:
    """Returns a prepared result, optionally holding the run at the search step until released."""

    def __init__(self, ready: RetrievalResult) -> None:
        self.ready = ready
        self.gate: asyncio.Event | None = None
        self.requests: list[RetrievalRequest] = []

    def set_passage(self, text: str) -> None:
        """Change the text of every passage returned, in the chunk and in its parent clause."""
        for retrieved in self.ready.chunks:
            retrieved.text = text
        for parent_clause in self.ready.parents.values():
            parent_clause.text = text

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        return self.ready


@dataclass
class Chat:
    service: ChatService
    conversations: ConversationService
    llm: FakeLLMProvider
    publisher: InMemoryPublisher
    retriever: GatedRetriever
    memory: MemoryManager
    principal: Principal
    conversation_id: UUID
    document_id: UUID
    uow_factory: UowFactory
    redis: Redis
    env: str
    weak: RetrievalResult = field(default_factory=weak_retrieval)

    async def messages(self) -> list[Message]:
        async with self.uow_factory() as uow:
            statement = (
                select(Message)
                .where(Message.conversation_id == self.conversation_id)
                .order_by(Message.seq)
            )
            return list((await uow.session.execute(statement)).scalars().all())

    async def conversation(self) -> Conversation:
        async with self.uow_factory() as uow:
            found = await uow.conversations.get(self.principal.scope(), self.conversation_id)
            assert found is not None
            return found

    async def lock_held(self) -> bool:
        from covenantiq.core import redis_keys

        return bool(
            await self.redis.exists(redis_keys.conversation_lock(self.env, self.conversation_id))
        )


def retrieval_for(
    document_id: UUID, owner: UUID, clause_id: UUID, chunk_id: UUID
) -> RetrievalResult:
    best = chunk(SOURCE_TEXT, rerank=0.9, dense=1, lexical=1, document=document_id)
    best.chunk_id, best.clause_id = chunk_id, clause_id
    second = chunk(
        "Liability for fees is capped.", rerank=0.6, dense=2, lexical=2, document=document_id
    )
    return result([best, second], parents=[parent(best, SOURCE_TEXT)])


@dataclass
class Parts:
    """The chat service and the fakes behind it, for any test that needs to script a reply."""

    service: ChatService
    memory: MemoryManager
    llm: FakeLLMProvider
    publisher: InMemoryPublisher
    retriever: GatedRetriever


async def seed_document(
    uow_factory: UowFactory, scope: OwnershipScope, title: str = "Acme MSA"
) -> tuple[UUID, UUID, UUID]:
    """A ready document with one clause and one chunk. Returns their ids."""
    async with uow_factory() as uow:
        document = await uow.documents.add(
            scope, make_document(scope.owner_id, status=DocumentStatus.READY, title=title)
        )
        clause = make_clause(scope.owner_id, document.id, 0, SOURCE_TEXT, clause_ref="6.2")
        uow.session.add(clause)
        await uow.session.flush()
        piece = make_chunk(scope.owner_id, document.id, clause.id, 0, SOURCE_TEXT)
        uow.session.add(piece)
        await uow.session.flush()
        ids = (document.id, clause.id, piece.id)
        await uow.commit()
    return ids


def make_parts(
    uow_factory: UowFactory,
    redis: Redis,
    env: str,
    ready: RetrievalResult,
    *,
    lock_redis: Redis | None = None,
    stale_after: timedelta = timedelta(minutes=10),
    heartbeat_seconds: float = 30.0,
    lock_ttl: int = 120,
) -> tuple[Parts, ConversationService]:
    memory = build(uow_factory, redis, env)
    conversations = ConversationService(uow_factory, memory.manager)
    llm = FakeLLMProvider()
    llm.script("answer", lambda r: GOOD_ANSWER)
    llm.script("classify", lambda r: {"intent": "document_qa", "is_follow_up": False})
    publisher = InMemoryPublisher()
    retriever = GatedRetriever(ready)
    rag = RagConfig()
    runner = GraphRunner(
        GraphDeps(
            retrieval=retriever,
            llm=llm,
            routes=RouteTable({}, "fake-model"),
            memory=memory.manager,
            evidence=EvidenceAssessor(rag.evidence),
            citations=CitationValidator(rag.citations),
            chat_store=PostgresChatStore(uow_factory),
            rag=rag,
        ),
        publisher,
    )
    service = ChatService(
        conversations,
        runner,
        ConversationLock(lock_redis or redis, env=env, ttl_seconds=lock_ttl),
        uow_factory,
        heartbeat_seconds=heartbeat_seconds,
        stale_after=stale_after,
    )
    return Parts(service, memory.manager, llm, publisher, retriever), conversations


async def build_chat(
    uow_factory: UowFactory,
    redis: Redis,
    env: str,
    *,
    lock_redis: Redis | None = None,
    stale_after: timedelta = timedelta(minutes=10),
    heartbeat_seconds: float = 30.0,
    lock_ttl: int = 120,
) -> Chat:
    scope: OwnershipScope = await new_user(uow_factory)
    principal = Principal(user_id=scope.owner_id, email="chat@example.com")
    document_id, clause_id, chunk_id = await seed_document(uow_factory, scope)
    parts, conversations = make_parts(
        uow_factory,
        redis,
        env,
        retrieval_for(document_id, scope.owner_id, clause_id, chunk_id),
        lock_redis=lock_redis,
        stale_after=stale_after,
        heartbeat_seconds=heartbeat_seconds,
        lock_ttl=lock_ttl,
    )
    created = await conversations.create(
        principal, title="Chat", document_ids=[document_id], vendor_id=None
    )
    return Chat(
        service=parts.service,
        conversations=conversations,
        llm=parts.llm,
        publisher=parts.publisher,
        retriever=parts.retriever,
        memory=parts.memory,
        principal=principal,
        conversation_id=created.id,
        document_id=document_id,
        uow_factory=uow_factory,
        redis=redis,
        env=env,
    )
