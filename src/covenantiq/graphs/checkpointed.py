"""Running a checkpointed graph for one job: start it, or resume it where a dead worker left off.

One checkpoint thread per job (`thread_id = job_id`). A run that finds an unfinished thread resumes
it (`astream(None)`), so steps that already finished are not repeated, and not paid for again. A
finished job's checkpoints are deleted. The analysis graphs (contract, vendor assessment) share
this; each supplies its graph builder and what to do with its custom progress events.
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph

from covenantiq.core.errors import PermanentTaskError
from covenantiq.core.logging import get_logger
from covenantiq.graphs.checkpointing import psycopg_url

log = get_logger(__name__)

RECURSION_LIMIT = 100
CheckpointerFactory = Callable[[], AbstractAsyncContextManager[BaseCheckpointSaver[Any]]]
GraphBuilder = Callable[[BaseCheckpointSaver[Any]], CompiledStateGraph[Any, Any, Any, Any]]
CustomEventHandler = Callable[[dict[str, Any]], Awaitable[None]]


def postgres_checkpointer(url: str) -> CheckpointerFactory:
    def make() -> AbstractAsyncContextManager[BaseCheckpointSaver[Any]]:
        return cast(
            AbstractAsyncContextManager[BaseCheckpointSaver[Any]],
            AsyncPostgresSaver.from_conn_string(psycopg_url(url)),
        )

    return make


@dataclass
class GraphRun:
    values: dict[str, Any]
    resumed: bool


async def run_checkpointed(
    *,
    build: GraphBuilder,
    checkpointer: CheckpointerFactory,
    thread_id: str,
    initial: dict[str, Any],
    max_concurrency: int,
    on_custom: CustomEventHandler,
    graph_name: str,
) -> GraphRun:
    config: Any = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
        "max_concurrency": max_concurrency,
    }
    async with checkpointer() as saver:
        graph = build(saver)
        snapshot = await graph.aget_state(config)
        resumed = bool(snapshot.next)
        if resumed:
            log.info(
                "analysis.resuming",
                job_id=thread_id,
                graph=graph_name,
                waiting_on=list(snapshot.next),
            )
        if resumed or not snapshot.values:
            async for item in graph.astream(
                None if resumed else initial, config, stream_mode=["custom", "updates"]
            ):
                mode, chunk = cast(tuple[str, Any], item)
                if mode == "custom":
                    await on_custom(chunk)
        values = dict((await graph.aget_state(config)).values)
        error = values.get("error")
        if error:
            raise PermanentTaskError(error["message"], job_code=error["code"])
        await saver.adelete_thread(thread_id)  # finished: its checkpoints are not needed
    return GraphRun(values, resumed)
