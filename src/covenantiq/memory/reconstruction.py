"""Rebuilding a conversation's window from PostgreSQL (spec 23.7).

Uses only database data, so it is deterministic and always possible: when Redis loses a session
(expiry, eviction, restart) or disagrees with the database, the window is simply rebuilt.

    1. take the stored summary and `summary_through_seq`
    2. read the completed messages after that point, newest first, until the token budget is full
    3. older ones that are still unsummarised are returned as `overflow`, for a summarising pass

Only finished exchanges are memory. A question whose reply is still being written (or failed, or was
interrupted) is left out: the reply in progress is recorded together with its question when it
completes, and a question nobody answered is not something a later turn should build on.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from covenantiq.core.enums import MessageRole, MessageStatus
from covenantiq.db.models import Message
from covenantiq.db.scope import OwnershipScope
from covenantiq.db.uow import UnitOfWork
from covenantiq.ingestion.chunking.tokens import estimate_tokens
from covenantiq.memory.base import ConversationState, SessionTurn

PAGE_SIZE = 100
MAX_MESSAGES = 600  # a hard cap on how far back one rebuild reads


@dataclass
class Reconstruction:
    turns: list[SessionTurn]  # the window, oldest first
    overflow: list[SessionTurn] = field(default_factory=list)  # unsummarised, older than the window


def to_turn(message: Message) -> SessionTurn | None:
    """A completed user or assistant message as a window turn; anything else is not memory."""
    if message.status is not MessageStatus.COMPLETED or not message.content.strip():
        return None
    if message.role not in (MessageRole.USER, MessageRole.ASSISTANT):
        return None
    return SessionTurn(
        seq=message.seq,
        role="user" if message.role is MessageRole.USER else "assistant",
        content=message.content,
        ts=message.completed_at or message.created_at,
        tokens=estimate_tokens(message.content),
    )


def split_window(
    turns: list[SessionTurn], budget: int
) -> tuple[list[SessionTurn], list[SessionTurn]]:
    """Fit the newest turns into `budget` tokens. The newest two are always kept, even if they are
    longer than the budget on their own: a window must never be empty of the last exchange.
    Returns (window, older), both oldest first."""
    kept = 0
    used = 0
    for turn in reversed(turns):
        if kept >= 2 and used + turn.tokens > budget:
            break
        used += turn.tokens
        kept += 1
    cut = len(turns) - kept
    return turns[cut:], turns[:cut]


class Reconstructor:
    def __init__(self, uow_factory: Callable[[], UnitOfWork], window_tokens: int) -> None:
        self._uow_factory = uow_factory
        self._window_tokens = window_tokens

    async def state(self, scope: OwnershipScope, conversation_id: UUID) -> ConversationState | None:
        async with self._uow_factory() as uow:
            conversation = await uow.conversations.get(scope, conversation_id)
            if conversation is None:
                return None
            return ConversationState(
                conversation_id=conversation.id,
                last_completed_seq=conversation.last_completed_seq,
                summary=conversation.summary,
                summary_through_seq=conversation.summary_through_seq,
            )

    async def reconstruct(self, scope: OwnershipScope, state: ConversationState) -> Reconstruction:
        turns: list[SessionTurn] = []
        answered: set[UUID] = set()  # user messages that have a completed reply
        before: int | None = None
        async with self._uow_factory() as uow:
            while len(turns) < MAX_MESSAGES:
                page = await uow.messages.recent(
                    scope, state.conversation_id, PAGE_SIZE, before_seq=before
                )
                if not page:
                    break
                before = page[0].seq
                # replies are newer than their questions, so they have already been seen
                answered |= {
                    m.reply_to_id
                    for m in page
                    if m.role is MessageRole.ASSISTANT
                    and m.status is MessageStatus.COMPLETED
                    and m.reply_to_id is not None
                }
                fresh = [m for m in page if m.seq > state.summary_through_seq]
                turns[:0] = [
                    t
                    for m in fresh
                    if (m.role is not MessageRole.USER or m.id in answered)
                    and (t := to_turn(m)) is not None
                ]
                if len(fresh) < len(page) or len(page) < PAGE_SIZE:
                    break  # reached what the summary already covers, or the start
        window, overflow = split_window(turns, self._window_tokens)
        return Reconstruction(window, overflow)
