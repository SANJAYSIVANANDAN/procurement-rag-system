"""The vendor assessment graph's nodes (spec 22.6). Thin: each reads state, calls one engine or
port, and returns a partial update. What decides a score is in `analysis/vendor`, which is pure."""

import asyncio
import re
from typing import Any
from uuid import UUID

from langgraph.config import get_stream_writer
from langgraph.graph import END
from langgraph.types import Send

from covenantiq.ai.llm.base import ChatMessage
from covenantiq.ai.prompts import load_prompt
from covenantiq.ai.schemas import DimensionExplanation, SignalExtraction, SignalExtractionBatch
from covenantiq.analysis.contract.types import SourceRef
from covenantiq.analysis.vendor.aggregation import aggregate_all
from covenantiq.analysis.vendor.config_schema import SignalSpec
from covenantiq.analysis.vendor.scoring import score_assessment
from covenantiq.analysis.vendor.signals import FindingFacts, map_contract_signals
from covenantiq.analysis.vendor.types import (
    EVIDENCE_BASED,
    AssessmentScore,
    DimensionScore,
    EvidenceRef,
    Observation,
    SignalAggregate,
    SignalStatus,
)
from covenantiq.core.enums import DimensionStatus, RiskDimension, Severity
from covenantiq.core.errors import CovenantError
from covenantiq.core.logging import get_logger
from covenantiq.graphs.contract_analysis.nodes import counts, merge_results, to_source
from covenantiq.graphs.errors import graph_error
from covenantiq.graphs.vendor_assessment.deps import (
    AssessmentRecord,
    DocumentAnalysis,
    VendorDeps,
    VendorScope,
)
from covenantiq.graphs.vendor_assessment.state import VendorState
from covenantiq.rag.context import build_context, escape_attribute, escape_text
from covenantiq.rag.types import RetrievalRequest

log = get_logger(__name__)

NOT_FOUND = "VENDOR_NOT_FOUND"
NO_DOCUMENTS = "NO_DOCUMENTS"
DEPENDENCY_NOT_READY = "DEPENDENCY_NOT_READY"
K_FINAL_PER_QUERY = 8
MAX_MITIGATIONS = 5
EVIDENCE_PER_DIMENSION = 6
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_SIGNAL_ID = re.compile(r"\b[A-Z]{2,4}-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")


def _error(code: str, message: str, retryable: bool, node: str) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": retryable, "node": node}


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


class SignalSources:
    """What retrieval found for one extraction signal."""

    def __init__(self, spec: SignalSpec) -> None:
        self.spec = spec
        self.sources: list[SourceRef] = []
        self.context_text = ""
        self.best_rerank: float | None = None
        self.error: str | None = None


class VendorNodes:
    def __init__(self, deps: VendorDeps) -> None:
        self._d = deps

    # ---- scope and dependencies -------------------------------------------------------------

    async def load_vendor_scope(self, state: VendorState) -> dict[str, Any]:
        get_stream_writer()({"event": "progress", "stage": "loading", "fraction": 0.02})
        scope = await self._d.store.load_scope(UUID(state["owner_id"]), UUID(state["vendor_id"]))
        if scope is None:
            return {"error": _error(NOT_FOUND, "The vendor no longer exists.", False, "load")}
        if not scope.documents:
            return {
                "error": _error(
                    NO_DOCUMENTS, "The vendor has no ready documents to assess.", False, "load"
                )
            }
        return {"scope": scope.model_dump(mode="json")}

    async def ensure_contract_findings(self, state: VendorState) -> dict[str, Any]:
        """Verify only (spec 22.6 step 2): the analyses were created before this graph started."""
        scope = VendorScope.model_validate(state["scope"])
        analyses = await self._d.store.document_analyses(
            UUID(state["owner_id"]), [d.id for d in scope.documents], self._d.ruleset_version
        )
        waiting = [a for a in analyses if a.state in ("pending", "missing")]
        if waiting:
            return {
                "error": _error(
                    DEPENDENCY_NOT_READY,
                    f"{len(waiting)} document(s) do not have a finished contract analysis yet.",
                    True,
                    "ensure_contract_findings",
                )
            }
        titles = {d.id: d.title for d in scope.documents}
        notes = [
            f"The contract analysis of '{titles[a.document_id]}' failed: its contract-based "
            "signals count as insufficient evidence."
            for a in analyses
            if a.state == "failed"
        ]
        return {"analyses": [a.model_dump(mode="json") for a in analyses], "notes": notes}

    async def map_findings_to_signals(self, state: VendorState) -> dict[str, Any]:
        get_stream_writer()({"event": "progress", "stage": "mapping_signals", "fraction": 0.1})
        scope = VendorScope.model_validate(state["scope"])
        analyses = [DocumentAnalysis.model_validate(a) for a in state["analyses"]]
        facts = {
            a.document_id: [
                FindingFacts(f.rule_id, f.status, f.severity, f.evidence) for f in a.findings
            ]
            for a in analyses
            if a.state == "current"
        }
        failed = {a.document_id for a in analyses if a.state == "failed"}
        observations = map_contract_signals(
            self._d.config.signals.signals, [d.id for d in scope.documents], facts, failed
        )
        return {"contract_observations": [o.model_dump(mode="json") for o in observations]}

    def fan_out(self, state: VendorState) -> list[Send] | str:
        if state.get("error"):
            return END
        shared = {k: state[k] for k in ("job_id", "owner_id", "vendor_id", "scope") if k in state}
        return [
            Send("extract_dimension_signals", {**shared, "dimension": d.value})
            for d in RiskDimension
        ]

    # ---- the signals a model reads ----------------------------------------------------------

    async def extract_dimension_signals(self, payload: dict[str, Any]) -> dict[str, Any]:
        d = self._d
        dimension = RiskDimension(payload["dimension"])
        specs = [s for s in d.config.signals.for_dimension(dimension) if not s.from_contract]
        if not specs:
            return {}
        scope = VendorScope.model_validate(payload["scope"])
        retrieval_scope = d.scope_for(UUID(payload["owner_id"]))
        gathered = [await self._gather(spec, scope, retrieval_scope) for spec in specs]
        extractions, usage, note = await self._ask_model(scope, gathered)
        observations: list[Observation] = []
        for g in gathered:
            observations.extend(self._observations(g, extractions.get(g.spec.id), scope))
        get_stream_writer()({"event": "progress", "stage": "reading_signals", "fraction": 0.4})
        return {
            "extracted_observations": [o.model_dump(mode="json") for o in observations],
            "usage": usage,
            "notes": [note] if note else [],
        }

    async def _gather(
        self, spec: SignalSpec, scope: VendorScope, retrieval_scope: Any
    ) -> SignalSources:
        """Retrieval across every one of the vendor's documents for this signal's queries."""
        found = SignalSources(spec)
        extraction = spec.extraction()
        config = self._d.rag.analysis
        try:
            results = [
                await self._d.retrieval.retrieve(
                    RetrievalRequest(
                        scope=retrieval_scope,
                        query=query,
                        document_ids=[doc.id for doc in scope.documents],
                        k_final=K_FINAL_PER_QUERY,
                    )
                )
                for query in extraction.queries
            ]
        except Exception:
            log.error("vendor.retrieval_failed", signal=spec.id, exc_info=True)
            found.error = "RETRIEVAL_FAILED"
            return found
        scores = [c.rerank_score for r in results for c in r.chunks if c.rerank_score is not None]
        found.best_rerank = max(scores) if scores else None
        merged = merge_results(results, config.max_sources_per_rule)
        bundle = build_context(
            merged,
            max_tokens=config.context_tokens_per_rule,
            parent_max_tokens=self._d.rag.context.parent_max_tokens,
        )
        by_chunk = {c.chunk_id: c.rerank_score for c in merged.chunks}
        found.sources = [to_source(s, by_chunk) for s in bundle.sources]
        found.context_text = bundle.text
        return found

    async def _ask_model(
        self, scope: VendorScope, gathered: list[SignalSources]
    ) -> tuple[dict[str, SignalExtraction], dict[str, int], str | None]:
        askable = [g for g in gathered if g.error is None]
        if not askable:
            return {}, {}, "signal extraction skipped: retrieval failed"
        blocks = []
        for g in askable:
            extraction = g.spec.extraction()
            blocks.append(
                f'<signal id="{g.spec.id}" label="{escape_attribute(g.spec.label)}">\n'
                f"<question>{escape_text(extraction.question)}</question>\n"
                f"<sources>\n{g.context_text or '(no passages were found)'}\n</sources>\n</signal>"
            )
        prompt = f'<vendor name="{escape_attribute(scope.name)}"/>\n\n' + "\n\n".join(blocks)
        request = self._d.routes.request(
            "signal_extraction", load_prompt("signal_extraction.v1"), [ChatMessage("user", prompt)]
        )
        try:
            result = await self._d.llm.structured(request, SignalExtractionBatch)
        except (CovenantError, ValueError) as exc:
            log.warning("vendor.extraction_failed", error=type(exc).__name__)
            return {}, {}, f"signal extraction failed ({graph_error(exc, 'extract').code})"
        return {e.signal_id: e for e in result.value.extractions}, counts(result.usage), None

    def _observations(
        self, gathered: SignalSources, extraction: SignalExtraction | None, scope: VendorScope
    ) -> list[Observation]:
        spec = gathered.spec

        def everywhere(status: SignalStatus, note: str) -> list[Observation]:
            return [
                Observation(signal_id=spec.id, document_id=doc.id, status=status, note=note)
                for doc in scope.documents
            ]

        if gathered.error or extraction is None:
            return everywhere("insufficient_evidence", "the signal could not be assessed")
        sources = {s.marker: s for s in gathered.sources}
        markers = [m for m in dict.fromkeys(extraction.supporting_markers) if m in sources]
        if extraction.status in EVIDENCE_BASED:
            if not markers:
                return everywhere(
                    "insufficient_evidence", "the model cited no passage it was given"
                )
            cited = [sources[m] for m in markers]
            if not self._d.citations.supports(
                extraction.rationale, " ".join(s.text for s in cited)
            )[0]:
                return everywhere(
                    "insufficient_evidence", "the rationale is not supported by the cited passages"
                )
            severity = (
                Severity(extraction.unfavorable_severity or "medium")
                if extraction.status == "unfavorable"
                else None
            )
            by_document: dict[UUID, list[SourceRef]] = {}
            for source in cited:
                by_document.setdefault(source.document_id, []).append(source)
            return [
                Observation(
                    signal_id=spec.id,
                    document_id=document_id,
                    status=extraction.status,
                    severity=severity,
                    evidence=[self._evidence(s, extraction.rationale) for s in group],
                    note=_clip(extraction.rationale, 300),
                )
                for document_id, group in sorted(by_document.items(), key=lambda kv: str(kv[0]))
            ]
        if extraction.status == "absent":
            complete = all(doc.extraction_ok for doc in scope.documents)
            weak = (
                gathered.best_rerank is None
                or gathered.best_rerank < self._d.rag.analysis.absent_threshold
            )
            if complete and weak:
                return everywhere("absent", "no passage in any document addresses it")
            return everywhere(
                "insufficient_evidence",
                "an absence cannot be established: the text was not extracted completely or "
                "the search found related passages",
            )
        return everywhere("insufficient_evidence", "the model could not tell")

    def _evidence(self, source: SourceRef, rationale: str) -> EvidenceRef:
        return EvidenceRef(
            document_id=source.document_id,
            document_title=source.document_title,
            quote=self._d.citations.quote_for(source.text, [rationale]),
            page_start=source.page_start,
            page_end=source.page_end,
            clause_ref=source.clause_ref,
            chunk_id=source.chunk_ids[0] if source.chunk_ids else None,
            clause_id=source.clause_id,
        )

    # ---- the deterministic part -------------------------------------------------------------

    async def validate_signal_evidence(self, state: VendorState) -> dict[str, Any]:
        """Every evidence-based observation must carry evidence; one that does not is downgraded."""
        if state.get("error"):
            return {}
        merged = [
            *state.get("contract_observations", []),
            *state.get("extracted_observations", []),
        ]
        checked: list[dict[str, Any]] = []
        downgraded = 0
        for raw in merged:
            observation = Observation.model_validate(raw)
            if observation.status in EVIDENCE_BASED and not observation.evidence:
                downgraded += 1
                observation = observation.model_copy(
                    update={
                        "status": "insufficient_evidence",
                        "severity": None,
                        "note": "no evidence was attached",
                    }
                )
            checked.append(observation.model_dump(mode="json"))
        notes = (
            [f"{downgraded} observation(s) were downgraded for lack of evidence"]
            if downgraded
            else []
        )
        return {"observations": checked, "notes": notes}

    async def aggregate_across_documents(self, state: VendorState) -> dict[str, Any]:
        get_stream_writer()({"event": "progress", "stage": "aggregating", "fraction": 0.55})
        observations = [Observation.model_validate(o) for o in state.get("observations", [])]
        aggregates = aggregate_all(self._d.config.signals.signals, observations)
        return {"aggregates": {k: v.model_dump(mode="json") for k, v in aggregates.items()}}

    async def score_deterministic(self, state: VendorState) -> dict[str, Any]:
        get_stream_writer()({"event": "progress", "stage": "scoring", "fraction": 0.65})
        aggregates = {k: SignalAggregate.model_validate(v) for k, v in state["aggregates"].items()}
        score = score_assessment(self._d.config.scoring, self._d.config.signals, aggregates)
        return {"score": score.model_dump(mode="json")}

    # ---- explanation ------------------------------------------------------------------------

    async def explain_dimensions(self, state: VendorState) -> dict[str, Any]:
        get_stream_writer()({"event": "progress", "stage": "explaining", "fraction": 0.8})
        score = AssessmentScore.model_validate(state["score"])
        aggregates = {k: SignalAggregate.model_validate(v) for k, v in state["aggregates"].items()}
        results = await asyncio.gather(*[self._explain(d, aggregates) for d in score.dimensions])
        explanations: dict[str, Any] = {}
        usage: dict[str, int] = {}
        notes: list[str] = []
        for dimension, (reasoning, mitigations, used, note) in zip(
            score.dimensions, results, strict=True
        ):
            explanations[dimension.dimension.value] = {
                "reasoning": reasoning,
                "mitigations": mitigations,
            }
            for key, value in used.items():
                usage[key] = usage.get(key, 0) + value
            if note:
                notes.append(f"{dimension.dimension.value}: {note}")
        return {"explanations": explanations, "usage": usage, "notes": notes}

    async def _explain(
        self, dimension: DimensionScore, aggregates: dict[str, SignalAggregate]
    ) -> tuple[str, list[str], dict[str, int], str | None]:
        """The model's words about computed results, or the template if they do not check out."""
        context = _dimension_context(dimension, aggregates)
        request = self._d.routes.request(
            "explain", load_prompt("explain_dimension.v1"), [ChatMessage("user", context)]
        )
        try:
            result = await self._d.llm.structured(request, DimensionExplanation)
        except (CovenantError, ValueError):
            return (
                template_reasoning(dimension),
                [],
                {},
                "the explanation fell back to the template (model unavailable)",
            )
        used = counts(result.usage)
        problem = _reasoning_problem(result.value.reasoning, dimension, context)
        if problem:
            return (
                template_reasoning(dimension),
                [],
                used,
                f"the explanation fell back to the template ({problem})",
            )
        risky = {s.signal_id for s in dimension.signals if s.status in ("unfavorable", "ambiguous")}
        mitigations = [
            _clip(m.text, 300)
            for m in result.value.mitigations
            if m.signal_ids and set(m.signal_ids) <= risky and m.text.strip()
        ][:MAX_MITIGATIONS]
        return _clip(result.value.reasoning, 1200), mitigations, used, None

    async def persist_assessment(self, state: VendorState) -> dict[str, Any]:
        get_stream_writer()({"event": "progress", "stage": "saving", "fraction": 0.95})
        scope = VendorScope.model_validate(state["scope"])
        score = AssessmentScore.model_validate(state["score"])
        aggregates = {k: SignalAggregate.model_validate(v) for k, v in state["aggregates"].items()}
        config = self._d.config
        signals_json: dict[str, list[dict[str, Any]]] = {}
        evidence: dict[str, list[EvidenceRef]] = {}
        for dimension in score.dimensions:
            rows: list[dict[str, Any]] = []
            quotes: list[EvidenceRef] = []
            for signal in sorted(dimension.signals, key=lambda s: (-s.weight, s.signal_id)):
                row = signal.model_dump(mode="json")
                aggregate = aggregates.get(signal.signal_id)
                row["observations"] = [
                    {
                        "document_id": str(o.document_id),
                        "status": o.status,
                        "severity": o.severity.value if o.severity else None,
                    }
                    for o in (aggregate.observations if aggregate else [])
                ]
                rows.append(row)
                if aggregate is not None:
                    quotes.extend(aggregate.evidence[:2])
            signals_json[dimension.dimension.value] = rows
            evidence[dimension.dimension.value] = quotes[:EVIDENCE_PER_DIMENSION]
        explanations = state.get("explanations", {})
        record = AssessmentRecord(
            vendor_id=scope.vendor_id,
            scoring_config_version=config.scoring.version,
            weights={d.value: w for d, w in config.scoring.dimension_weights.items()},
            documents=[{"id": str(d.id), "title": d.title} for d in scope.documents],
            score=score,
            dimension_signals=signals_json,
            reasoning={k: v["reasoning"] for k, v in explanations.items()},
            mitigations={k: v["mitigations"] for k, v in explanations.items()},
            evidence=evidence,
            notes=state.get("notes", []),
        )
        assessment_id = await self._d.store.persist_assessment(
            UUID(state["owner_id"]), UUID(state["job_id"]), record
        )
        get_stream_writer()({"event": "progress", "stage": "done", "fraction": 1.0})
        return {"assessment_id": str(assessment_id)}


# ---- helpers ------------------------------------------------------------------------------------


def template_reasoning(dimension: DimensionScore) -> str:
    """The deterministic explanation: what the numbers and statuses already say."""
    label = dimension.dimension.value.replace("_", " ")
    if dimension.status is DimensionStatus.NOT_ASSESSED or dimension.score is None:
        return f"The {label} dimension could not be assessed: no signal had usable evidence."
    unfavorable = [s.signal_id for s in dimension.signals if s.status == "unfavorable"]
    ambiguous = [s.signal_id for s in dimension.signals if s.status == "ambiguous"]
    included = sum(1 for s in dimension.signals if s.risk is not None)
    parts = [
        f"The {label} risk score is {dimension.score:.0f} ({dimension.severity.value}), based on "
        f"{included} of {len(dimension.signals)} signals ({dimension.coverage:.0%} coverage)."
    ]
    if dimension.status is DimensionStatus.INSUFFICIENT_EVIDENCE:
        parts.append("Coverage is below the minimum, so treat this score as indicative only.")
    if unfavorable:
        parts.append("Unfavorable: " + ", ".join(unfavorable) + ".")
    if ambiguous:
        parts.append("Unclear: " + ", ".join(ambiguous) + ".")
    if dimension.missing_information:
        parts.append(
            "Not assessed for lack of evidence: "
            + ", ".join(m.signal_id for m in dimension.missing_information)
            + "."
        )
    return " ".join(parts)


def _dimension_context(dimension: DimensionScore, aggregates: dict[str, SignalAggregate]) -> str:
    score = "none" if dimension.score is None else f"{dimension.score:.1f}"
    lines = [
        f'<dimension name="{dimension.dimension.value}" status="{dimension.status.value}" '
        f'score="{score}" severity="{dimension.severity.value}" '
        f'coverage_percent="{dimension.coverage * 100:.0f}"/>'
    ]
    for signal in dimension.signals:
        aggregate = aggregates.get(signal.signal_id)
        quotes = "".join(
            f'\n  <quote document="{escape_attribute(e.document_title)}">'
            f"{escape_text(_clip(e.quote, 240))}</quote>"
            for e in (aggregate.evidence[:2] if aggregate else [])
        )
        state = signal.gap or signal.status
        lines.append(
            f'<signal id="{signal.signal_id}" label="{escape_attribute(signal.label)}" '
            f'status="{state}" severity="{signal.severity.value if signal.severity else ""}" '
            f'conflict="{str(signal.conflict).lower()}">{quotes}</signal>'
        )
    return "\n".join(lines)


def _reasoning_problem(reasoning: str, dimension: DimensionScore, context: str) -> str | None:
    """Why a model explanation cannot be used, or None. It may name only signals of this
    dimension and may not introduce a number the engine did not compute."""
    known = {s.signal_id for s in dimension.signals}
    for token in _SIGNAL_ID.findall(reasoning):
        if token not in known:
            return f"it names unknown signal {token}"
    allowed = set(_NUMBER.findall(context))
    for number in _NUMBER.findall(reasoning):
        if number not in allowed:
            return f"it states a number that was not computed ({number})"
    if not reasoning.strip():
        return "it is empty"
    return None
