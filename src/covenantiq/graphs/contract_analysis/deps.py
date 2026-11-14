"""What the contract analysis graph depends on (spec 22.2): engines and ports, no database.

State is checkpointed to PostgreSQL after every step, so everything that travels through it is a
plain JSON value (ids as strings, models as dicts). The caller's ownership scope is rebuilt from
the owner id by `scope_for`, supplied by the service, because graphs may not import `db`.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from covenantiq.ai.llm.base import LLMProvider
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.analysis.contract.rules_loader import Ruleset
from covenantiq.analysis.contract.types import FindingDraft
from covenantiq.graphs.deps import RetrieverPort, Scope
from covenantiq.rag.citations import CitationValidator
from covenantiq.rag.config import RagConfig


class DocumentInfo(BaseModel):
    id: UUID
    title: str
    status: str
    contract_type: str
    extraction_quality: dict[str, Any] = Field(default_factory=dict)
    pages_total: int | None = None
    clause_types_present: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class ChunkText(BaseModel):
    chunk_id: UUID
    clause_id: UUID | None
    document_id: UUID
    document_title: str
    text: str
    clause_ref: str | None = None
    page_start: int | None = None
    page_end: int | None = None


class AnalysisStorePort(Protocol):
    async def load_document(self, owner_id: UUID, document_id: UUID) -> DocumentInfo | None: ...

    async def document_chunks(self, owner_id: UUID, document_id: UUID) -> list[ChunkText]: ...

    async def persist(
        self,
        owner_id: UUID,
        job_id: UUID,
        document_id: UUID,
        findings: Sequence[FindingDraft],
        stats: dict[str, Any],
    ) -> None:
        """Replace the job's findings (delete, then insert, with their citations) and mark the job
        succeeded, in one transaction. Running it twice leaves one set of findings."""
        ...


class ProgressPort(Protocol):
    async def report(self, job_id: UUID, stage: str, fraction: float) -> None: ...


@dataclass(frozen=True)
class AnalysisDeps:
    retrieval: RetrieverPort
    llm: LLMProvider
    routes: RouteTable
    store: AnalysisStorePort
    ruleset: Ruleset
    rag: RagConfig
    citations: CitationValidator
    scope_for: Callable[[UUID], Scope]
