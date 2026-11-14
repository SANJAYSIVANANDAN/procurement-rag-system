"""What the vendor comparison graph depends on (spec 22.7): engines and ports, no database."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from covenantiq.ai.llm.base import LLMProvider
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.analysis.vendor.comparison import AssessmentSnapshot
from covenantiq.analysis.vendor.config_schema import VendorConfig


class LoadedAssessment(BaseModel):
    """A vendor's latest assessment, as the comparison needs it, with a few evidence quotes per
    dimension for the narrative."""

    snapshot: AssessmentSnapshot
    scoring_config_version: str
    quotes: dict[str, list[str]] = Field(default_factory=dict)


class ComparisonStorePort(Protocol):
    async def load_assessments(
        self, owner_id: UUID, vendor_ids: Sequence[UUID]
    ) -> list[LoadedAssessment]:
        """The latest succeeded assessment of each vendor that has one."""
        ...

    async def persist_comparison(
        self,
        owner_id: UUID,
        job_id: UUID,
        comparison_id: UUID,
        result: dict[str, Any],
        narrative: str,
    ) -> None:
        """Store the result and mark the job succeeded in one transaction."""
        ...


class ProgressPort(Protocol):
    async def report(self, job_id: UUID, stage: str, fraction: float) -> None: ...


@dataclass(frozen=True)
class ComparisonDeps:
    llm: LLMProvider
    routes: RouteTable
    store: ComparisonStorePort
    config: VendorConfig
