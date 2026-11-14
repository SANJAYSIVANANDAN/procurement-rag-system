import operator
from typing import Annotated, Any, TypedDict

from covenantiq.graphs.chat.state import add_counts, merge_dicts


class AnalysisState(TypedDict, total=False):
    # inputs
    job_id: str
    owner_id: str
    document_id: str
    # understanding the document and the rules
    document: dict[str, Any]
    quality: dict[str, Any]  # {"ok": bool, "text_ratio": float}
    rule_ids: list[str]
    batches: list[list[str]]
    contract_type_note: str | None
    # results; the reducers let parallel branches each add to them
    rule_results: Annotated[list[dict[str, Any]], operator.add]
    validated_results: list[dict[str, Any]]
    obligations: list[dict[str, Any]]
    contradictions: list[dict[str, Any]]
    unusual: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    stats: dict[str, Any]
    usage: Annotated[dict[str, int], add_counts]
    warnings: Annotated[list[str], operator.add]
    timings_ms: Annotated[dict[str, int], merge_dicts]
    error: dict[str, Any] | None
