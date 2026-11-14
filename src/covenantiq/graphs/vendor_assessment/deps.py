"""What the vendor assessment graph depends on (spec 22.2, 22.6): engines and ports, no database.

State is checkpointed, so everything here is a plain JSON model. The owner's scope is rebuilt from
the owner id by `scope_for`, since graphs may not import `db`.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from covenantiq.ai.llm.base import LLMProvider
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.analysis.vendor.config_schema import VendorConfig
from covenantiq.analysis.vendor.types import AssessmentScore, EvidenceRef
from covenantiq.core.enums import ClauseStatus, Severity
from covenantiq.graphs.deps import RetrieverPort, Scope
from covenantiq.rag.citations import CitationValidator
from covenantiq.rag.config import RagConfig


class VendorDocument(BaseModel):
    id: UUID
    title: str
    extraction_ok: bool = True  # the text was extracted completely enough to claim an absence
    updated_at: datetime | None = None


class VendorScope(BaseModel):
    vendor_id: UUID
    name: str
    documents: list[VendorDocument]  # ready and not deleted


class FindingFact(BaseModel):
    rule_id: str
    status: ClauseStatus
    severity: Severity
    evidence: list[EvidenceRef] = Field(default_factory=list)


class DocumentAnalysis(BaseModel):
    """Where a document's contract analysis stands, with its findings if it has a current one."""

    document_id: UUID
    state: Literal["current", "pending", "failed", "missing"]
    job_id: UUID | None = None
    findings: list[FindingFact] = Field(default_factory=list)


class AssessmentRecord(BaseModel):
    """Everything one assessment stores, computed and explained."""

    vendor_id: UUID
    scoring_config_version: str
    weights: dict[str, float]
    documents: list[dict[str, str]]
    score: AssessmentScore
    dimension_signals: dict[str, list[dict[str, Any]]]
    reasoning: dict[str, str]
    mitigations: dict[str, list[str]]
    evidence: dict[str, list[EvidenceRef]]
    notes: list[str] = Field(default_factory=list)


class VendorStorePort(Protocol):
    async def load_scope(self, owner_id: UUID, vendor_id: UUID) -> VendorScope | None: ...

    async def document_analyses(
        self, owner_id: UUID, document_ids: Sequence[UUID], ruleset_version: str
    ) -> list[DocumentAnalysis]: ...

    async def persist_assessment(
        self, owner_id: UUID, job_id: UUID, record: AssessmentRecord
    ) -> UUID:
        """Store the assessment, its six dimensions and their citations, flip `is_latest`, and
        mark the job succeeded: one transaction. Running it twice leaves one assessment."""
        ...


class ProgressPort(Protocol):
    async def report(self, job_id: UUID, stage: str, fraction: float) -> None: ...


@dataclass(frozen=True)
class VendorDeps:
    retrieval: RetrieverPort
    llm: LLMProvider
    routes: RouteTable
    store: VendorStorePort
    config: VendorConfig
    ruleset_version: str  # the contract ruleset a document's analysis must have used
    rag: RagConfig
    citations: CitationValidator
    scope_for: Callable[[UUID], Scope]
