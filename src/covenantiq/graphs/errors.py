"""Errors inside a graph run, and the safe words the user sees for them."""

import asyncio
import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from covenantiq.ai.llm.base import LLMBadRequest, LLMRefused, LLMTruncated, LLMUnavailable
from covenantiq.core.errors import TransientError
from covenantiq.core.logging import get_logger

log = get_logger(__name__)

NodeFn = Callable[[Any], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class GraphError:
    code: str
    message: str  # safe to show a user: no provider text, no prompt, no stack trace
    retryable: bool
    node: str


def graph_error(exc: BaseException, node: str) -> GraphError:
    """Map an exception to what the user is told. The detail goes to the log, not to the reply."""
    if isinstance(exc, LLMUnavailable):
        return GraphError(
            "LLM_UNAVAILABLE",
            "The answering service is busy or unreachable right now. Please try again.",
            True,
            node,
        )
    if isinstance(exc, LLMTruncated):
        return GraphError(
            "LLM_TRUNCATED",
            "The answer was cut off because it was too long. Try asking about a narrower point.",
            False,
            node,
        )
    if isinstance(exc, LLMRefused):
        return GraphError("LLM_REFUSED", "The model declined to answer this request.", False, node)
    if isinstance(exc, LLMBadRequest):
        return GraphError(
            "LLM_MISCONFIGURED",
            "The answering service is not set up correctly. Please contact an administrator.",
            False,
            node,
        )
    if isinstance(exc, TransientError):
        return GraphError(
            "DEPENDENCY_UNAVAILABLE",
            "A service this answer depends on is unavailable. Please try again.",
            True,
            node,
        )
    return GraphError(
        "INTERNAL_ERROR", "Something went wrong while preparing the answer.", True, node
    )


def node_guard(name: str) -> Callable[[NodeFn], NodeFn]:
    """Wrap a node so a failure becomes `state.error` (routing the run to `finalize`) instead of
    an exception that would end the graph with nothing recorded. Also records the node's time."""

    def decorate(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        async def guarded(state: Any) -> dict[str, Any]:
            started = monotonic()
            try:
                update = dict(await fn(state))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("chat.node_failed", node=name, error=type(exc).__name__, exc_info=True)
                update = {"error": graph_error(exc, name)}
            update["timings_ms"] = {
                **update.get("timings_ms", {}),
                name: int((monotonic() - started) * 1000),
            }
            return update

        return guarded

    return decorate
