"""The vendor assessment graph (spec 22.6).

    load_vendor_scope -> ensure_contract_findings -> map_findings_to_signals
        -> Send(extract_dimension_signals) x 6 -> validate_signal_evidence
        -> aggregate_across_documents -> score_deterministic -> explain_dimensions
        -> persist_assessment

Checkpointed per job like the contract analysis. The contract analyses it relies on are created
before it starts (by the service); inside the graph the dependency step only verifies, and fails
with a retryable DEPENDENCY_NOT_READY if one is still missing.
"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from covenantiq.graphs.contract_analysis.graph import guard
from covenantiq.graphs.vendor_assessment.deps import VendorDeps
from covenantiq.graphs.vendor_assessment.nodes import VendorNodes
from covenantiq.graphs.vendor_assessment.state import VendorState

GRAPH_NAME = "vendor_assessment_graph.v1"


def _stop_on_error(next_node: str) -> Any:
    def route(state: VendorState) -> str:
        return END if state.get("error") else next_node

    return route


def build_vendor_graph(
    deps: VendorDeps, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> CompiledStateGraph[Any, Any, Any, Any]:
    nodes = VendorNodes(deps)
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(VendorState)
    for name in (
        "load_vendor_scope",
        "ensure_contract_findings",
        "map_findings_to_signals",
        "extract_dimension_signals",
        "validate_signal_evidence",
        "aggregate_across_documents",
        "score_deterministic",
        "explain_dimensions",
        "persist_assessment",
    ):
        step: Any = guard(name)(getattr(nodes, name))
        graph.add_node(name, step)

    graph.add_edge(START, "load_vendor_scope")
    graph.add_conditional_edges(
        "load_vendor_scope",
        _stop_on_error("ensure_contract_findings"),
        ["ensure_contract_findings", END],
    )
    graph.add_conditional_edges(
        "ensure_contract_findings",
        _stop_on_error("map_findings_to_signals"),
        ["map_findings_to_signals", END],
    )
    graph.add_conditional_edges(
        "map_findings_to_signals", nodes.fan_out, ["extract_dimension_signals", END]
    )
    graph.add_edge("extract_dimension_signals", "validate_signal_evidence")
    graph.add_conditional_edges(
        "validate_signal_evidence",
        _stop_on_error("aggregate_across_documents"),
        ["aggregate_across_documents", END],
    )
    graph.add_edge("aggregate_across_documents", "score_deterministic")
    graph.add_edge("score_deterministic", "explain_dimensions")
    graph.add_edge("explain_dimensions", "persist_assessment")
    graph.add_edge("persist_assessment", END)
    return graph.compile(checkpointer=checkpointer)
