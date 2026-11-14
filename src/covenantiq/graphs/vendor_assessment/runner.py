"""Running the vendor assessment graph for one job (spec 22.6). Checkpoint, resume and cleanup are
shared (`graphs/checkpointed.py`); here only the progress is specific."""

from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import UUID

from covenantiq.core.errors import PermanentTaskError
from covenantiq.core.logging import get_logger
from covenantiq.graphs.checkpointed import CheckpointerFactory, run_checkpointed
from covenantiq.graphs.vendor_assessment.deps import ProgressPort, VendorDeps
from covenantiq.graphs.vendor_assessment.graph import GRAPH_NAME, build_vendor_graph

log = get_logger(__name__)


@dataclass
class AssessmentOutcome:
    assessment_id: UUID
    resumed: bool


class VendorAssessmentRunner:
    def __init__(
        self,
        deps: VendorDeps,
        progress: ProgressPort,
        checkpointer: CheckpointerFactory,
        *,
        max_concurrency: int = 3,
        progress_interval: float = 2.0,
    ) -> None:
        self._deps = deps
        self._progress = progress
        self._checkpointer = checkpointer
        self._max_concurrency = max_concurrency
        self._interval = progress_interval

    async def run(self, *, job_id: UUID, owner_id: UUID, vendor_id: UUID) -> AssessmentOutcome:
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
                log.warning("vendor.progress_failed", exc_info=True)

        run = await run_checkpointed(
            build=lambda saver: build_vendor_graph(self._deps, saver),
            checkpointer=self._checkpointer,
            thread_id=str(job_id),
            initial={"job_id": str(job_id), "owner_id": str(owner_id), "vendor_id": str(vendor_id)},
            max_concurrency=self._max_concurrency,
            on_custom=handle,
            graph_name=GRAPH_NAME,
        )
        assessment_id = run.values.get("assessment_id")
        if not assessment_id:
            raise PermanentTaskError(
                "The assessment ended without a result.", job_code="ASSESSMENT_INCOMPLETE"
            )
        log.info("vendor.assessed", job_id=str(job_id), graph=GRAPH_NAME)
        return AssessmentOutcome(UUID(assessment_id), run.resumed)
