"""The chat graph (spec 22.4).

    load_context -> classify_intent -+-> respond_direct ------------------------------+
                                     +-> abstain (no documents to search) ------------+
                                     +-> rewrite_query -> retrieve -> assess_evidence |
                                           -+-> abstain -----------------------------+|
                                            +-> generate_grounded -> validate_citations
                                                   -+-> finalize <--------------------+
                                                    +-> abstain -> finalize

Every fallible node can set `state.error`; the edge after it then goes straight to `finalize`,
which stores the message as failed (with whatever text was already streamed) and emits `error`.
No checkpointing (ADR-005): the durable state of a chat turn is its message rows.
"""

from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from covenantiq.core.enums import GroundingStatus
from covenantiq.graphs.chat.nodes import DIRECT_INTENTS, ChatNodes
from covenantiq.graphs.chat.state import ChatState
from covenantiq.graphs.deps import GraphDeps
from covenantiq.graphs.errors import node_guard

GRAPH_NAME = "chat_graph.v1"


def _after(next_node: str) -> Any:
    def route(state: ChatState) -> str:
        return "finalize" if state.get("error") else next_node

    return route


def _after_classify(state: ChatState) -> str:
    if state.get("error"):
        return "finalize"
    if state["intent"] in DIRECT_INTENTS:
        return "respond_direct"
    if not state["document_ids"]:
        return "abstain"  # nothing to search: say so rather than answer from nowhere
    return "rewrite_query"


def _after_evidence(state: ChatState) -> str:
    if state.get("error"):
        return "finalize"
    return "generate_grounded" if state["evidence"].sufficient else "abstain"


def _after_validation(state: ChatState) -> str:
    if state.get("error"):
        return "finalize"
    return "abstain" if state.get("grounding_status") is GroundingStatus.ABSTAINED else "finalize"


def build_chat_graph(deps: GraphDeps) -> CompiledStateGraph[Any, Any, Any, Any]:
    nodes = ChatNodes(deps)
    graph: StateGraph[Any, Any, Any, Any] = StateGraph(ChatState)
    for name in (
        "load_context",
        "classify_intent",
        "respond_direct",
        "rewrite_query",
        "retrieve",
        "assess_evidence",
        "generate_grounded",
        "validate_citations",
        "abstain",
    ):
        step: Any = node_guard(name)(getattr(nodes, name))
        graph.add_node(name, step)
    graph.add_node("finalize", cast(Any, nodes.finalize))  # finalize handles its own failures

    graph.add_edge(START, "load_context")
    graph.add_conditional_edges("load_context", _after("classify_intent"))
    graph.add_conditional_edges("classify_intent", _after_classify)
    graph.add_conditional_edges("respond_direct", _after("finalize"))
    graph.add_conditional_edges("rewrite_query", _after("retrieve"))
    graph.add_conditional_edges("retrieve", _after("assess_evidence"))
    graph.add_conditional_edges("assess_evidence", _after_evidence)
    graph.add_conditional_edges("generate_grounded", _after("validate_citations"))
    graph.add_conditional_edges("validate_citations", _after_validation)
    graph.add_conditional_edges("abstain", _after("finalize"))
    graph.add_edge("finalize", END)
    return graph.compile()
