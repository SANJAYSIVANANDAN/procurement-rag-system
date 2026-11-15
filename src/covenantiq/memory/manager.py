"""The memory manager: one entry point for the chat graph (spec 23.9, 23.5).

    load          the window and summary of a conversation, rebuilt from PostgreSQL if stale
    record_turn   append a finished exchange; if the window overflowed, summarise in the background

Summarising never blocks a reply. It runs as a background task: summarise (the previous summary plus
the turns that overflowed), write the summary to PostgreSQL and commit, and only then mirror it into
Redis. If summarising fails, the session is dropped from Redis, so the next load rebuilds it from
PostgreSQL and finds the still-unsummarised turns again. Nothing is lost, because every turn is in
PostgreSQL.
"""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID

from covenantiq.ai.llm.base import LLMRefused, LLMUnavailable
from covenantiq.core.logging import get_logger
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.memory.base import (
    CompletedTurn,
    ConversationState,
    MemoryContext,
    SessionTurn,
)
from covenantiq.memory.session_redis import SessionMemoryProvider
from covenantiq.memory.summarizer import Summarizer

log = get_logger(__name__)

Spawn = Callable[[Coroutine[Any, Any, None]], "asyncio.Task[None]"]


class MemoryManager:
    def __init__(
        self,
        session: SessionMemoryProvider,
        summarizer: Summarizer,
        uow_factory: Callable[[], UnitOfWork],
        *,
        spawn: Spawn | None = None,
    ) -> None:
        self._session = session
        self._summarizer = summarizer
        self._uow_factory = uow_factory
        self._spawn: Spawn = spawn or asyncio.ensure_future
        self._tasks: set[asyncio.Task[None]] = set()
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def load(self, scope: OwnershipScope, state: ConversationState) -> MemoryContext:
        """The caller has already checked, in PostgreSQL, that the conversation is the user's."""
        loaded = await self._session.load_session(scope, state)
        if loaded.overflow:
            self._schedule(scope, state.conversation_id, loaded.overflow)
        return loaded.context

    async def record_turn(
        self, scope: OwnershipScope, conversation_id: UUID, turn: CompletedTurn
    ) -> None:
        result = await self._session.record_turn(scope, conversation_id, turn)
        if result.overflow:
            self._schedule(scope, conversation_id, result.overflow)

    async def invalidate(self, scope: OwnershipScope, conversation_id: UUID) -> None:
        """Drop the session from Redis; the next load rebuilds it from PostgreSQL."""
        await self._session.invalidate(scope, conversation_id)

    # ---- summarising ------------------------------------------------------------------------

    def _schedule(
        self, scope: OwnershipScope, conversation_id: UUID, overflow: list[SessionTurn]
    ) -> None:
        task = self._spawn(self._summarize(scope, conversation_id, overflow))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _summarize(
        self, scope: OwnershipScope, conversation_id: UUID, overflow: list[SessionTurn]
    ) -> None:
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:  # one summary at a time per conversation: each builds on the last
            try:
                await self._summarize_locked(scope, conversation_id, overflow)
            except (LLMUnavailable, LLMRefused):
                log.warning("memory.summary_failed", conversation_id=str(conversation_id))
                await self._session.invalidate(scope, conversation_id)
            except Exception:
                log.error(
                    "memory.summary_error", conversation_id=str(conversation_id), exc_info=True
                )
                await self._session.invalidate(scope, conversation_id)

    async def _summarize_locked(
        self, scope: OwnershipScope, conversation_id: UUID, overflow: list[SessionTurn]
    ) -> None:
        async with self._uow_factory() as uow:
            conversation = await uow.conversations.get(scope, conversation_id)
            if conversation is None:
                return
            previous = conversation.summary
            already = conversation.summary_through_seq
        fresh = [turn for turn in overflow if turn.seq > already]  # an earlier pass may cover some
        if not fresh:
            return
        summary = await self._summarizer.summarize(previous, fresh)
        through = fresh[-1].seq

        async with self._uow_factory() as uow:  # PostgreSQL first, and committed...
            stored = await uow.conversations.advance_summary(
                scope, conversation_id, summary, through
            )
            await uow.commit()
        if stored:  # ...then Redis
            await self._session.set_summary(scope, conversation_id, summary, through)
        log.info("memory.summarised", conversation_id=str(conversation_id), through_seq=through)

    async def aclose(self, wait_seconds: float = 10.0) -> None:
        """Let summaries that are in flight finish (on shutdown)."""
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=wait_seconds)
