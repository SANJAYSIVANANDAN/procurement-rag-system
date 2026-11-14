"""Shared setup for the memory integration tests: real PostgreSQL, real Redis, a fake model."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis

from covenantiq.ai.llm.fake import FakeLLMProvider
from covenantiq.core.enums import MessageRole, MessageStatus
from covenantiq.db.models import Conversation, Message
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.ingestion.chunking.tokens import estimate_tokens
from covenantiq.memory import (
    CompletedTurn,
    ConversationState,
    MemoryManager,
    Reconstructor,
    SessionMemoryProvider,
    SessionTurn,
    Summarizer,
)
from tests.integration.factories import make_user

UowFactory = Callable[[], UnitOfWork]


@dataclass
class Memory:
    provider: SessionMemoryProvider
    manager: MemoryManager
    reconstructor: Reconstructor
    llm: FakeLLMProvider


def build(
    uow_factory: UowFactory,
    redis: Redis,
    env: str,
    *,
    ttl: int = 600,
    window_tokens: int = 3000,
    summary_tokens: int = 600,
) -> Memory:
    llm = FakeLLMProvider()
    reconstructor = Reconstructor(uow_factory, window_tokens)
    provider = SessionMemoryProvider(
        redis, reconstructor, env=env, ttl_seconds=ttl, window_tokens=window_tokens
    )
    manager = MemoryManager(provider, Summarizer(llm, summary_tokens=summary_tokens), uow_factory)
    return Memory(provider, manager, reconstructor, llm)


async def new_user(uow_factory: UowFactory, email: str | None = None) -> OwnershipScope:
    async with uow_factory() as uow:
        user = await uow.users.add(make_user(email))
        await uow.commit()
    return OwnershipScope(user.id)


async def new_conversation(uow_factory: UowFactory, scope: OwnershipScope) -> UUID:
    async with uow_factory() as uow:
        conversation = await uow.conversations.create(
            scope, Conversation(owner_id=scope.owner_id, title="Chat")
        )
        await uow.commit()
    return conversation.id


def turn(seq: int, role: str, content: str) -> SessionTurn:
    return SessionTurn(
        seq=seq,
        role=role,  # type: ignore[arg-type]
        content=content,
        ts=datetime.now(UTC),
        tokens=estimate_tokens(content),
    )


async def exchange(
    uow_factory: UowFactory,
    scope: OwnershipScope,
    conversation_id: UUID,
    question: str,
    answer: str,
    *,
    complete: bool = True,
) -> CompletedTurn:
    """One question and its answer, stored the way the chat service will store them: a user
    message and a pending assistant message in one transaction, then the assistant finalised."""
    async with uow_factory() as uow:
        conversation = await uow.conversations.get_for_update(scope, conversation_id)
        assert conversation is not None
        user, assistant = await uow.messages.add_pair(
            scope,
            conversation,
            Message(role=MessageRole.USER, content=question, status=MessageStatus.COMPLETED),
            Message(role=MessageRole.ASSISTANT, status=MessageStatus.PENDING),
        )
        user_seq, assistant_seq, assistant_id = user.seq, assistant.seq, assistant.id
        await uow.commit()
    if complete:
        async with uow_factory() as uow:
            await uow.messages.complete(
                scope, assistant_id, content=answer, status=MessageStatus.COMPLETED
            )
            await uow.commit()
    return CompletedTurn(turn(user_seq, "user", question), turn(assistant_seq, "assistant", answer))


async def state_of(
    memory: Memory, scope: OwnershipScope, conversation_id: UUID
) -> ConversationState:
    state = await memory.reconstructor.state(scope, conversation_id)
    assert state is not None
    return state


def texts(context_turns: list[SessionTurn]) -> list[str]:
    return [t.content for t in context_turns]


def shape(context_turns: list[SessionTurn]) -> list[tuple[int, str, str, int]]:
    """What makes two windows the same window. Timestamps are left out on purpose: a turn recorded
    live carries the caller's clock, a rebuilt one the database's `completed_at`."""
    return [(t.seq, t.role, t.content, t.tokens) for t in context_turns]
