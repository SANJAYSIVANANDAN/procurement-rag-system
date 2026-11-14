"""Runs the chat graph and carries its events to the stream (spec 22.2, 22.3, 26.8).

Everything a user sees while a reply is written comes from `writer(...)` calls inside graph nodes.
The runner validates each as an SSE event model, stamps it with the message id, checks the ordering
guarantee, and publishes it. It also guarantees what the nodes cannot: a run that ends without a
terminal event (a bug, a crash in `finalize`) still ends with exactly one, and the message is not
left streaming.
"""

import asyncio
from collections.abc import Sequence
from time import monotonic
from typing import Any, cast
from uuid import UUID

import structlog

from covenantiq.core.enums import MessageStatus
from covenantiq.core.logging import get_logger
from covenantiq.graphs.chat.graph import GRAPH_NAME, build_chat_graph
from covenantiq.graphs.deps import ChatOutcome, GraphDeps, Scope
from covenantiq.streaming.events import SSEEvent, is_terminal, parse_event
from covenantiq.streaming.publisher import StreamPublisher

log = get_logger(__name__)

RECURSION_LIMIT = 40  # the graph is acyclic; this only guards against a wiring mistake


class GraphRunner:
    def __init__(self, deps: GraphDeps, publisher: StreamPublisher) -> None:
        self._deps = deps
        self._publisher = publisher
        self._chat = build_chat_graph(deps)

    async def run_chat(
        self,
        *,
        scope: Scope,
        conversation_id: UUID,
        user_message_id: UUID,
        assistant_message_id: UUID,
        question: str,
        document_ids: Sequence[UUID],
    ) -> None:
        """Generate the reply to one question. Never raises for a failure inside the run: that is
        recorded on the message and published as the `error` event. Cancellation propagates."""
        message_id = assistant_message_id
        terminal = False
        opened = False
        started = monotonic()

        async def emit(raw: dict[str, Any]) -> None:
            nonlocal terminal, opened
            event: SSEEvent = parse_event({"message_id": message_id, **raw})
            if terminal:
                log.warning("chat.event_after_terminal", kind=event.event)
                return
            if not opened and event.event != "message_start":
                # a run that failed before it began: open the stream so the order still holds
                opened = True
                await self._publisher.publish(
                    message_id,
                    parse_event(
                        {
                            "message_id": message_id,
                            "event": "message_start",
                            "conversation_id": conversation_id,
                            "seq": 0,
                        }
                    ),
                )
            opened = True
            await self._publisher.publish(message_id, event)
            terminal = is_terminal(event)

        with structlog.contextvars.bound_contextvars(
            conversation_id=str(conversation_id), message_id=str(message_id), graph=GRAPH_NAME
        ):
            try:
                seq = await self._deps.chat_store.mark_streaming(scope, message_id)
                await emit(
                    {"event": "message_start", "conversation_id": conversation_id, "seq": seq}
                )
                state = {
                    "scope": scope,
                    "conversation_id": conversation_id,
                    "user_message_id": user_message_id,
                    "assistant_message_id": message_id,
                    "question": question,
                    "document_ids": list(document_ids),
                }
                async for item in self._chat.astream(
                    state,
                    config={"recursion_limit": RECURSION_LIMIT},
                    stream_mode=["custom", "updates"],
                ):
                    mode, chunk = cast(tuple[str, Any], item)
                    if mode == "custom":
                        await emit(chunk)
                    else:
                        for node, update in chunk.items():
                            timing = (update or {}).get("timings_ms", {}).get(node)
                            log.debug("chat.node_done", node=node, ms=timing)
            except asyncio.CancelledError:
                # Shutdown: say so once, mark the message interrupted, and let the cancel through.
                await asyncio.shield(self._interrupted(scope, message_id, emit, terminal))
                raise
            except Exception:
                log.error("chat.run_failed", exc_info=True)
            if not terminal:
                await self._fail(scope, message_id, emit)
            log.info("chat.run_finished", total_ms=int((monotonic() - started) * 1000))

    async def _fail(self, scope: Scope, message_id: UUID, emit: Any) -> None:
        """The run ended without a terminal event: record the failure and say so, once."""
        try:
            await self._deps.chat_store.complete(
                scope,
                message_id,
                ChatOutcome(
                    content="", status=MessageStatus.FAILED, grounding_status=None, intent=None
                ),
            )
        except Exception:
            log.error("chat.fail_persist_failed", exc_info=True)
        try:
            await emit(
                {
                    "event": "error",
                    "code": "INTERNAL_ERROR",
                    "message": "Something went wrong while preparing the answer.",
                    "retryable": True,
                }
            )
        except Exception:
            log.error("chat.fail_publish_failed", exc_info=True)

    async def _interrupted(self, scope: Scope, message_id: UUID, emit: Any, terminal: bool) -> None:
        try:
            await self._deps.chat_store.mark_interrupted(scope, message_id)
            if not terminal:
                await emit(
                    {
                        "event": "error",
                        "code": "INTERRUPTED",
                        "message": "The server restarted before the answer was finished.",
                        "retryable": True,
                    }
                )
        except Exception:
            log.error("chat.interrupt_failed", exc_info=True)
