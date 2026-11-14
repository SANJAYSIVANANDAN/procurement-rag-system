"""Starting a chat reply (spec 22, 26.3, 26.5).

`submit` is the whole POST: check the conversation is the caller's, answer an idempotent replay
without generating anything, take the per-conversation lock, store the question and an empty reply,
and start the producer as a background task. The producer runs the chat graph, so the reply
continues if the client disconnects, and always ends by releasing the lock.

The lock is taken before anything is stored, so when Redis is down nothing is persisted (503), and
when a reply is already being written the second question is refused (409) rather than interleaved.
"""

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from redis.exceptions import RedisError

from covenantiq.core.enums import MessageStatus
from covenantiq.core.errors import GenerationInProgress, TransientError
from covenantiq.core.logging import get_logger
from covenantiq.db.uow import UnitOfWork
from covenantiq.graphs.runner import GraphRunner
from covenantiq.services.conversation_service import ConversationService
from covenantiq.services.dto import Principal, SubmittedMessageDTO
from covenantiq.streaming.locks import ConversationLock

log = get_logger(__name__)

HEARTBEAT_SECONDS = 30.0
STALE_AFTER = timedelta(minutes=10)  # longer than any reply can legitimately take


@dataclass(frozen=True)
class ChatSubmission:
    user_message_id: UUID
    assistant_message_id: UUID
    status: MessageStatus
    replay: bool  # the same client_message_id was sent before: nothing new was started


def _submission(dto: SubmittedMessageDTO) -> ChatSubmission:
    return ChatSubmission(
        dto.user_message_id, dto.assistant_message_id, dto.assistant_status, dto.replay
    )


class ChatService:
    def __init__(
        self,
        conversations: ConversationService,
        runner: GraphRunner,
        lock: ConversationLock,
        uow_factory: Callable[[], UnitOfWork],
        *,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        stale_after: timedelta = STALE_AFTER,
    ) -> None:
        self._conversations = conversations
        self._runner = runner
        self._lock = lock
        self._uow_factory = uow_factory
        self._heartbeat_seconds = heartbeat_seconds
        self._stale_after = stale_after
        self._producers: set[asyncio.Task[None]] = set()  # held so a task is never collected early

    async def submit(
        self,
        principal: Principal,
        conversation_id: UUID,
        *,
        content: str,
        client_message_id: UUID | None = None,
        document_ids: Sequence[UUID] | None = None,
    ) -> ChatSubmission:
        await self._conversations.ensure_owned(principal, conversation_id)  # 404 before anything
        if client_message_id is not None:
            earlier = await self._conversations.existing_submission(
                principal, conversation_id, client_message_id
            )
            if earlier is not None:
                return _submission(earlier)

        try:
            token = await self._lock.acquire(conversation_id)
        except (RedisError, OSError):
            log.error("chat.lock_unavailable", exc_info=True)
            raise TransientError("Chat is temporarily unavailable. Please try again.") from None
        if token is None:
            raise GenerationInProgress("A reply is already being written in this conversation.")

        try:
            submitted = await self._conversations.submit_message(
                principal,
                conversation_id,
                content=content,
                client_message_id=client_message_id,
                document_ids=document_ids,
            )
        except BaseException:
            await self._release(conversation_id, token)
            raise
        if submitted.replay:  # another request with this id won the race while we waited
            await self._release(conversation_id, token)
            return _submission(submitted)

        task = asyncio.create_task(
            self._produce(principal, conversation_id, submitted, content.strip(), token)
        )
        self._producers.add(task)
        task.add_done_callback(self._producers.discard)
        return _submission(submitted)

    async def _produce(
        self,
        principal: Principal,
        conversation_id: UUID,
        submitted: SubmittedMessageDTO,
        question: str,
        token: str,
    ) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(conversation_id, token))
        try:
            await self._runner.run_chat(
                scope=principal.scope(),
                conversation_id=conversation_id,
                user_message_id=submitted.user_message_id,
                assistant_message_id=submitted.assistant_message_id,
                question=question,
                document_ids=submitted.selected_document_ids,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("chat.producer_failed", exc_info=True)
        finally:
            heartbeat.cancel()
            await asyncio.shield(self._release(conversation_id, token))

    async def _heartbeat(self, conversation_id: UUID, token: str) -> None:
        """Keep the lock alive while a long reply is written; if the process dies, it expires."""
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                if not await self._lock.extend(conversation_id, token):
                    log.warning("chat.lock_lost", conversation_id=str(conversation_id))
                    return
            except (RedisError, OSError):
                log.warning("chat.heartbeat_failed")

    async def _release(self, conversation_id: UUID, token: str) -> None:
        try:
            await self._lock.release(conversation_id, token)
        except (RedisError, OSError):
            log.warning("chat.release_failed")  # it expires on its own

    # ---- lifecycle --------------------------------------------------------------------------

    async def idle(self) -> None:
        """Wait until no reply is being written (tests, and a clean shutdown of a script)."""
        if self._producers:
            await asyncio.gather(*list(self._producers), return_exceptions=True)

    async def aclose(self, wait_seconds: float = 20.0) -> None:
        """On shutdown: let replies in flight finish for up to `wait_seconds`; the rest are
        cancelled, which marks them interrupted and tells their listeners."""
        pending = list(self._producers)
        if not pending:
            return
        _, still_running = await asyncio.wait(pending, timeout=wait_seconds)
        for task in still_running:
            task.cancel()
        if still_running:
            await asyncio.gather(*still_running, return_exceptions=True)

    async def sweep_stale(self) -> int:
        """Mark replies that nobody is writing any more as interrupted (spec 26.5). Safe to run
        from every replica at once: the update only touches messages past the stale threshold."""
        async with self._uow_factory() as uow:
            count = await uow.messages.system_mark_interrupted_stale(
                datetime.now(UTC) - self._stale_after
            )
            await uow.commit()
        if count:
            log.warning("chat.stale_messages_interrupted", count=count)
        return count


async def stale_message_loop(chat: ChatService, interval_seconds: float) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        with contextlib.suppress(Exception):
            await chat.sweep_stale()
