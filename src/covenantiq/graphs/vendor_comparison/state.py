import operator
from typing import Annotated, Any, TypedDict

from covenantiq.graphs.chat.state import add_counts, merge_dicts


class ComparisonState(TypedDict, total=False):
    job_id: str
    owner_id: str
    comparison_id: str
    vendor_ids: list[str]
    loaded: list[dict[str, Any]]
    result: dict[str, Any]
    draft: str | None
    narrative: str
    narrative_source: str
    notes: Annotated[list[str], operator.add]
    usage: Annotated[dict[str, int], add_counts]
    timings_ms: Annotated[dict[str, int], merge_dicts]
    error: dict[str, Any] | None
