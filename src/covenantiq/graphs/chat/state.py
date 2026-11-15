from typing import Annotated, TypedDict
from uuid import UUID

from covenantiq.core.enums import GroundingStatus, QueryIntent
from covenantiq.graphs.deps import ChatCitation, Scope
from covenantiq.graphs.errors import GraphError
from covenantiq.memory.base import MemoryContext
from covenantiq.rag.types import ContextBundle, EvidenceVerdict, RetrievalResult


def merge_dicts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {**left, **right}


def add_counts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    merged = dict(left)
    for key, value in right.items():
        merged[key] = merged.get(key, 0) + value
    return merged


class ChatState(TypedDict, total=False):
    # inputs
    scope: Scope
    conversation_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    question: str
    document_ids: list[UUID]
    # memory
    memory: MemoryContext
    # understanding
    intent: QueryIntent
    is_follow_up: bool
    standalone_query: str
    keywords: list[str]
    phrases: list[str]
    # retrieval and evidence
    retrieval: RetrievalResult
    context: ContextBundle
    evidence: EvidenceVerdict
    # output
    draft_answer: str
    canary: str
    canary_leaked: bool
    final_answer: str
    citations: list[ChatCitation]
    grounding_status: GroundingStatus
    validation: dict[str, int]
    usage: Annotated[dict[str, int], add_counts]
    timings_ms: Annotated[dict[str, int], merge_dicts]
    error: GraphError | None
