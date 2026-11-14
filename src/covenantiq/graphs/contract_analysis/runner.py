"""Running the contract analysis graph for one job (spec 22.2, 22.5).

The checkpoint, resume and cleanup mechanics are shared (`graphs/checkpointed.py`). What is specific
here is the progress: custom events from the graph reach the job row at most once every two seconds.
"""

from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from uuid import UUID

from covenantiq.core.errors import PermanentTaskError
from covenantiq.core.logging import get_logger
from covenantiq.graphs.checkpointed import CheckpointerFactory, run_checkpointed
from covenantiq.graphs.contract_analysis.deps import AnalysisDeps, ProgressPort
from covenantiq.graphs.contract_analysis.graph import GRAPH_NAME, build_contract_graph

log = get_logger(__name__)

PROGRESS_INTERVAL = 2.0
BATCH_PROGRESS_START, BATCH_PROGRESS_END = 0.05, 0.75


@dataclass
class AnalysisOutcome:
    findings: int
    stats: dict[str, Any] = field(default_factory=dict)
    resumed: bool = False


class ContractAnalysisRunner:
    def __init__(
        self,
        deps: AnalysisDeps,
        progress: ProgressPort,
        checkpointer: CheckpointerFactory,
        *,
        max_concurrency: int = 3,
        progress_interval: float = PROGRESS_INTERVAL,
    ) -> None:
        self._deps = deps
        self._progress = progress
        self._checkpointer = checkpointer
        self._max_concurrency = max_concurrency
        self._interval = progress_interval

    async def run(self, *, job_id: UUID, owner_id: UUID, document_id: UUID) -> AnalysisOutcome:
        reporter = _Progress(self._progress, job_id, self._interval)
        run = await run_checkpointed(
            build=lambda saver: build_contract_graph(self._deps, saver),
            checkpointer=self._checkpointer,
            thread_id=str(job_id),
            initial={
                "job_id": str(job_id),
                "owner_id": str(owner_id),
                "document_id": str(document_id),
            },
            max_concurrency=self._max_concurrency,
            on_custom=reporter.handle,
            graph_name=GRAPH_NAME,
        )
        if "findings" not in run.values:
            raise PermanentTaskError(
                "The analysis ended without producing findings.", job_code="ANALYSIS_INCOMPLETE"
            )
        findings = run.values["findings"]
        log.info("analysis.finished", job_id=str(job_id), graph=GRAPH_NAME, findings=len(findings))
        return AnalysisOutcome(len(findings), run.values.get("stats", {}), run.resumed)


class _Progress:
    """Turns the graph's custom events into throttled job progress."""

    def __init__(self, port: ProgressPort, job_id: UUID, interval: float) -> None:
        self._port = port
        self._job_id = job_id
        self._interval = interval
        self._batches = 1
        self._done = 0
        self._last = 0.0

    async def handle(self, chunk: dict[str, Any]) -> None:
        kind = chunk.get("event")
        fraction: float | None = None
        stage: str | None = chunk.get("stage")
        if kind == "plan":
            self._batches = max(1, int(chunk["batches"]))
            stage, fraction = "assessing_rules", BATCH_PROGRESS_START
        elif kind == "batch_done":
            self._done += 1
            stage = "assessing_rules"
            span = BATCH_PROGRESS_END - BATCH_PROGRESS_START
            fraction = BATCH_PROGRESS_START + span * min(1.0, self._done / self._batches)
        elif kind == "progress":
            fraction = float(chunk["fraction"])
        if stage is None or fraction is None:
            return
        now = monotonic()
        if now - self._last < self._interval and fraction < 1.0:
            return
        self._last = now
        try:
            await self._port.report(self._job_id, stage, fraction)
        except Exception:
            log.warning("analysis.progress_failed", exc_info=True)  # never fails the analysis
