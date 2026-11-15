"""What the graphs depend on (spec 22.2): engines and ports, never sessions or SQL.

Graphs may not import `covenantiq.db` (an import-linter contract), so the caller's ownership scope
travels through them as an opaque value (`Scope`) that only the ports and engines look inside.
Anything that reaches PostgreSQL does so through `ChatPersistencePort`, implemented in `services`
with a unit of work per call.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from covenantiq.ai.llm.base import LLMProvider
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.core.enums import GroundingStatus, MessageStatus, QueryIntent
from covenantiq.memory.base import CompletedTurn, ConversationState, MemoryContext
from covenantiq.rag.citations import CitationValidator
from covenantiq.rag.config import RagConfig
from covenantiq.rag.evidence import EvidenceAssessor
from covenantiq.rag.types import RetrievalRequest, RetrievalResult

Scope = Any  # an `OwnershipScope`; opaque here because graphs never import db


@dataclass(frozen=True)
class ChatCitation:
    """A validated citation, with everything needed to store it and to show it."""

    marker: int
    quote: str
    document_id: UUID
    document_title: str
    clause_id: UUID | None
    chunk_id: UUID | None
    clause_ref: str | None
    page_start: int | None
    page_end: int | None


@dataclass
class ChatOutcome:
    """The final state of an assistant message, written in one transaction."""

    content: str
    status: MessageStatus  # completed | failed
    grounding_status: GroundingStatus | None
    intent: QueryIntent | None
    citations: list[ChatCitation] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredTurn:
    """The exchange as stored, for the memory window: the user message and its reply."""

    user_seq: int
    user_content: str
    user_at: datetime
    assistant_seq: int
    assistant_at: datetime


class ChatPersistencePort(Protocol):
    async def mark_streaming(self, scope: Scope, message_id: UUID) -> int:
        """The assistant message is being written. Returns its sequence number."""
        ...

    async def conversation_state(
        self, scope: Scope, conversation_id: UUID
    ) -> ConversationState: ...

    async def document_titles(
        self, scope: Scope, document_ids: Sequence[UUID]
    ) -> dict[UUID, str]: ...

    async def complete(self, scope: Scope, message_id: UUID, outcome: ChatOutcome) -> StoredTurn:
        """Update the assistant message, insert its citations and (for a completed message)
        advance the conversation, in one transaction that is committed before this returns."""
        ...

    async def mark_interrupted(self, scope: Scope, message_id: UUID) -> None: ...


class MemoryPort(Protocol):
    async def load(self, scope: Scope, state: ConversationState) -> MemoryContext: ...

    async def record_turn(
        self, scope: Scope, conversation_id: UUID, turn: CompletedTurn
    ) -> None: ...

    async def invalidate(self, scope: Scope, conversation_id: UUID) -> None: ...


class RetrieverPort(Protocol):
    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult: ...


@dataclass(frozen=True)
class GraphDeps:
    retrieval: RetrieverPort
    llm: LLMProvider
    routes: RouteTable
    memory: MemoryPort
    evidence: EvidenceAssessor
    citations: CitationValidator
    chat_store: ChatPersistencePort
    rag: RagConfig
