"""Running the vendor comparison graph for one job (spec 22.7)."""

from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import UUID

from covenantiq.core.logging import get_logger
from covenantiq.graphs.checkpointed import CheckpointerFactory, run_checkpointed
from covenantiq.graphs.vendor_comparison.deps import ComparisonDeps, ProgressPort
from covenantiq.graphs.vendor_comparison.graph import GRAPH_NAME, build_comparison_graph

log = get_logger(__name__)


@dataclass
class ComparisonOutcome:
    narrative_source: str
    resumed: bool


class VendorComparisonRunner:
    def __init__(
        self,
        deps: ComparisonDeps,
        progress: ProgressPort,
        checkpointer: CheckpointerFactory,
        *,
        progress_interval: float = 2.0,
    ) -> None:
        self._deps = deps
        self._progress = progress
        self._checkpointer = checkpointer
        self._interval = progress_interval

    async def run(
        self, *, job_id: UUID, owner_id: UUID, comparison_id: UUID, vendor_ids: list[UUID]
    ) -> ComparisonOutcome:
        last = 0.0

        async def handle(chunk: dict[str, Any]) -> None:
            nonlocal last
            if chunk.get("event") != "progress":
                return
            fraction = float(chunk["fraction"])
            now = monotonic()
            if now - last < self._interval and fraction < 1.0:
                return
            last = now
            try:
                await self._progress.report(job_id, str(chunk["stage"]), fraction)
            except Exception:
                log.warning("comparison.progress_failed", exc_info=True)

        run = await run_checkpointed(
            build=lambda saver: build_comparison_graph(self._deps, saver),
            checkpointer=self._checkpointer,
            thread_id=str(job_id),
            initial={
                "job_id": str(job_id),
                "owner_id": str(owner_id),
                "comparison_id": str(comparison_id),
                "vendor_ids": [str(v) for v in vendor_ids],
            },
            max_concurrency=1,
            on_custom=handle,
            graph_name=GRAPH_NAME,
        )
        return ComparisonOutcome(run.values.get("narrative_source", "template"), run.resumed)
