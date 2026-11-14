import operator
from typing import Annotated, Any, TypedDict

from covenantiq.graphs.chat.state import add_counts, merge_dicts


class VendorState(TypedDict, total=False):
    job_id: str
    owner_id: str
    vendor_id: str
    scope: dict[str, Any]
    analyses: list[dict[str, Any]]
    contract_observations: list[dict[str, Any]]
    extracted_observations: Annotated[list[dict[str, Any]], operator.add]
    observations: list[dict[str, Any]]
    aggregates: dict[str, Any]
    score: dict[str, Any]
    explanations: dict[str, Any]
    assessment_id: str
    notes: Annotated[list[str], operator.add]
    usage: Annotated[dict[str, int], add_counts]
    timings_ms: Annotated[dict[str, int], merge_dicts]
    error: dict[str, Any] | None
