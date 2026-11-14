"""The contract analysis graph (spec 22.5).

    load_document_and_rules -> plan_rule_batches -> Send(assess_rule_batch) x N
        -> aggregate_rule_results -> extract_obligations -> detect_internal_contradictions
        -> scan_unusual_provisions -> validate_evidence -> build_findings -> persist_findings

Compiled with a PostgreSQL checkpointer, one thread per job: if the worker dies after four of seven
batches, the redelivered task resumes the same thread and the four finished batches are not run
again. A node that fails in a way no rule result can describe sets `state.error` and the run stops.
"""

import asyncio
import functools
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from covenantiq.core.logging import get_logger
from covenantiq.graphs.contract_analysis.deps import AnalysisDeps
from covenantiq.graphs.contract_analysis.nodes import ContractNodes
from covenantiq.graphs.contract_analysis.state import AnalysisState
from covenantiq.graphs.errors import graph_error

log = get_logger(__name__)

GRAPH_NAME = "contract_analysis_graph.v1"
NodeFn = Callable[[Any], Awaitable[dict[str, Any]]]


def guard(name: str) -> Callable[[NodeFn], NodeFn]:
    """Turn an exception into `state.error` and record the node's time. The error is a plain dict:
    state is checkpointed, and only JSON values survive being read back."""

    def decorate(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        async def guarded(state: Any) -> dict[str, Any]:
            started = monotonic()
            try:
                update = dict(await fn(state))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(
                    "analysis.node_failed", node=name, error=type(exc).__name__, exc_info=True
                )
                error = graph_error(exc, name)
                update = {
                    "error": {
                        "code": error.code,
                        "message": error.message,
                        "retryable": error.retryable,
                        "node": name,
                    }
                }
            update["timings_ms"] = {
                **update.get("timings_ms", {}),
                name: int((monotonic() - started) * 1000),
            }
            return update

        return guarded

    return decorate


def _stop_on_error(next_node: str) -> Callable[[AnalysisState], str]:
    def route(state: AnalysisState) -> str:
        return END if state.get("error") else next_node

    return route


def build_contract_graph(
    deps: AnalysisDeps, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> CompiledStateGraph[Any, Any, Any, Any]:
    nodes = ContractNodes(deps)
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(AnalysisState)
    names = (
        "load_document_and_rules",
        "plan_rule_batches",
        "assess_rule_batch",
        "aggregate_rule_results",
        "extract_obligations",
        "detect_internal_contradictions",
        "scan_unusual_provisions",
        "validate_evidence",
        "build_findings",
        "persist_findings",
    )
    for name in names:
        step: Any = guard(name)(getattr(nodes, name))
        graph.add_node(name, step)

    graph.add_edge(START, "load_document_and_rules")
    graph.add_conditional_edges(
        "load_document_and_rules", _stop_on_error("plan_rule_batches"), ["plan_rule_batches", END]
    )
    graph.add_conditional_edges("plan_rule_batches", nodes.fan_out, ["assess_rule_batch", END])
    graph.add_edge("assess_rule_batch", "aggregate_rule_results")
    graph.add_conditional_edges(
        "aggregate_rule_results",
        _stop_on_error("extract_obligations"),
        ["extract_obligations", END],
    )
    graph.add_edge("extract_obligations", "detect_internal_contradictions")
    graph.add_edge("detect_internal_contradictions", "scan_unusual_provisions")
    graph.add_edge("scan_unusual_provisions", "validate_evidence")
    graph.add_edge("validate_evidence", "build_findings")
    graph.add_conditional_edges(
        "build_findings", _stop_on_error("persist_findings"), ["persist_findings", END]
    )
    graph.add_edge("persist_findings", END)
    return graph.compile(checkpointer=checkpointer)
