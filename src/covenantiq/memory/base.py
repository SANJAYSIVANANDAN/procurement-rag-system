"""What conversation memory hands to the chat graph (spec 23.9).

Version 1 has one kind of memory: the Redis session window (recent turns plus a rolling summary),
with PostgreSQL as the durable source of truth. Losing Redis costs latency, never correctness.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

# A change to the stored turn or meta format bumps this; a mismatch rebuilds from PostgreSQL.
SCHEMA_VERSION = 1


class SessionTurn(BaseModel):
    """One message of the window, as stored in Redis. Only final content is ever stored."""

    model_config = ConfigDict(frozen=True)

    seq: int
    role: Literal["user", "assistant"]
    content: str
    ts: datetime
    tokens: int


@dataclass(frozen=True)
class CompletedTurn:
    """A user message and the assistant reply that finished it."""

    user: SessionTurn
    assistant: SessionTurn


@dataclass(frozen=True)
class ConversationState:
    """The conversation facts the caller already loaded to check ownership.

    Passing them in means the consistency check against `last_completed_seq` costs no extra query.
    """

    conversation_id: UUID
    last_completed_seq: int
    summary: str | None
    summary_through_seq: int


@dataclass
class MemoryContext:
    summary: str | None
    recent_turns: list[SessionTurn]
