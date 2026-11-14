"""What conversation memory hands to the chat graph (spec 23.9).

Memory is layered so later versions can add providers (semantic, episodic, entity) without touching
graph or service code: the manager merges every provider's contribution into `MemoryContext`, and
the prompt builder renders whatever is in `extensions`. Version 1 has one provider, the Redis
session window, with PostgreSQL as the durable source of truth: losing Redis costs latency, never
correctness.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from covenantiq.db.scope import OwnershipScope

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
    extensions: dict[str, Any] = field(default_factory=dict)  # V2: semantic, episodic, entities


class MemoryProvider(Protocol):
    name: str

    async def load(
        self, scope: OwnershipScope, state: ConversationState, query: str | None
    ) -> dict[str, Any]: ...

    async def record_turn(
        self, scope: OwnershipScope, conversation_id: UUID, turn: CompletedTurn
    ) -> None: ...
