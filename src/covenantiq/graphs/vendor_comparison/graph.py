"""The vendor comparison graph (spec 22.7).

    load_latest_assessments -> check_config_compatibility -> compute_comparability
        -> generate_narrative -> validate_narrative_references -> persist_comparison

A narrative failure never fails the job: the deterministic template summary is used instead.
"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from covenantiq.graphs.contract_analysis.graph import guard
from covenantiq.graphs.vendor_comparison.deps import ComparisonDeps
from covenantiq.graphs.vendor_comparison.nodes import ComparisonNodes
from covenantiq.graphs.vendor_comparison.state import ComparisonState

GRAPH_NAME = "vendor_comparison_graph.v1"


def _stop_on_error(next_node: str) -> Any:
    def route(state: ComparisonState) -> str:
        return END if state.get("error") else next_node

    return route


def build_comparison_graph(
    deps: ComparisonDeps, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> CompiledStateGraph[Any, Any, Any, Any]:
    nodes = ComparisonNodes(deps)
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(ComparisonState)
    names = (
        "load_latest_assessments",
        "check_config_compatibility",
        "compute_comparability",
        "generate_narrative",
        "validate_narrative_references",
        "persist_comparison",
    )
    for name in names:
        step: Any = guard(name)(getattr(nodes, name))
        graph.add_node(name, step)
    graph.add_edge(START, "load_latest_assessments")
    graph.add_conditional_edges(
        "load_latest_assessments",
        _stop_on_error("check_config_compatibility"),
        ["check_config_compatibility", END],
    )
    graph.add_conditional_edges(
        "check_config_compatibility",
        _stop_on_error("compute_comparability"),
        ["compute_comparability", END],
    )
    graph.add_edge("compute_comparability", "generate_narrative")
    graph.add_edge("generate_narrative", "validate_narrative_references")
    graph.add_edge("validate_narrative_references", "persist_comparison")
    graph.add_edge("persist_comparison", END)
    return graph.compile(checkpointer=checkpointer)
