"""The contract analysis graph's nodes (spec 22.5, 20.4 to 20.8). Thin: each reads state, calls one
engine or port, and returns a partial update. Everything that decides something is in
`analysis/contract`, which is pure and tested on its own."""

import asyncio
import re
from collections.abc import Sequence
from typing import Any, Literal
from uuid import UUID

from langgraph.config import get_stream_writer
from langgraph.graph import END
from langgraph.types import Send

from covenantiq.ai.llm.base import ChatMessage, LLMUsage
from covenantiq.ai.prompts import load_prompt
from covenantiq.ai.schemas import (
    ContradictionVerdict,
    ObligationExtraction,
    RuleAssessment,
    RuleAssessmentBatch,
)
from covenantiq.analysis.contract import findings as finding_builders
from covenantiq.analysis.contract import obligations as obligation_rules
from covenantiq.analysis.contract.contradictions import find_candidates, observe
from covenantiq.analysis.contract.heuristics import run_heuristics
from covenantiq.analysis.contract.rules_schema import RuleSpec
from covenantiq.analysis.contract.status import (
    Classification,
    Evidence,
    ExtractionQualityInfo,
    classify,
    confidence_for,
    severity_for,
    sufficiency_for,
)
from covenantiq.analysis.contract.types import (
    ContradictionRecord,
    FindingDraft,
    HeuristicHit,
    ObligationRecord,
    RuleResult,
    SourceRef,
    ValueObservation,
)
from covenantiq.core.enums import ClauseStatus, ContractType, EvidenceSufficiency, Severity
from covenantiq.core.errors import CovenantError
from covenantiq.core.logging import get_logger
from covenantiq.graphs.contract_analysis.deps import AnalysisDeps, DocumentInfo
from covenantiq.graphs.contract_analysis.state import AnalysisState
from covenantiq.graphs.errors import graph_error
from covenantiq.rag.context import build_context, escape_attribute, escape_text
from covenantiq.rag.types import (
    ContextSource,
    RetrievalRequest,
    RetrievalResult,
    RetrievedChunk,
)

log = get_logger(__name__)

NOT_READY = "DOCUMENT_NOT_READY"
DEGRADED = "ANALYSIS_DEGRADED"
EXPLANATION_FALLBACK = "The explanation could not be checked against the document."
_MARKER_NUMBER = re.compile(r"\d+")


def counts(usage: LLMUsage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
    }


def merge_results(results: Sequence[RetrievalResult], limit: int) -> RetrievalResult:
    """The best passages across several searches: duplicates collapsed, ranked by rerank score
    (then fused score), at most `limit`."""
    best: dict[UUID, RetrievedChunk] = {}
    for result in results:
        for chunk in result.chunks:
            current = best.get(chunk.chunk_id)
            if current is None or _rank(chunk) > _rank(current):
                best[chunk.chunk_id] = chunk
    chunks = sorted(best.values(), key=_rank, reverse=True)[:limit]
    parents: dict[UUID, Any] = {}
    neighbours: dict[UUID, list[RetrievedChunk]] = {}
    for result in results:
        parents.update(result.parents)
        neighbours.update(result.neighbours)
    return RetrievalResult(
        chunks=chunks, parents=parents, trace=results[0].trace, neighbours=neighbours
    )


def _rank(chunk: RetrievedChunk) -> tuple[float, float]:
    return (chunk.rerank_score if chunk.rerank_score is not None else -1.0, chunk.rrf_score)


def to_source(source: ContextSource, scores: dict[UUID, float | None]) -> SourceRef:
    ranked = [s for s in (scores.get(c) for c in source.chunk_ids) if s is not None]
    return SourceRef(
        marker=int(source.marker[1:]),
        chunk_ids=list(source.chunk_ids),
        clause_id=source.clause_id,
        document_id=source.document_id,
        document_title=source.document_title,
        clause_ref=source.clause_ref,
        page_start=source.page_start,
        page_end=source.page_end,
        text=source.text,
        rerank_score=max(ranked) if ranked else None,
    )


class Gathered:
    """What retrieval found for one rule."""

    def __init__(self, rule: RuleSpec) -> None:
        self.rule = rule
        self.sources: list[SourceRef] = []
        self.context_text = ""
        self.best_rerank: float | None = None
        self.queries_run = 0
        self.error: str | None = None


class ContractNodes:
    def __init__(self, deps: AnalysisDeps) -> None:
        self._d = deps

    # ---- plan -------------------------------------------------------------------------------

    async def load_document_and_rules(self, state: AnalysisState) -> dict[str, Any]:
        d = self._d
        get_stream_writer()({"event": "progress", "stage": "loading", "fraction": 0.02})
        owner, document_id = UUID(state["owner_id"]), UUID(state["document_id"])
        info = await d.store.load_document(owner, document_id)
        if info is None:
            return {"error": _error(NOT_READY, "The document no longer exists.", False, "load")}
        if info.status != "ready":
            return {
                "error": _error(
                    NOT_READY, "The document is not ready to be analysed.", False, "load"
                )
            }
        contract_type = ContractType(info.contract_type)
        rules = d.ruleset.applicable(contract_type)
        quality = ExtractionQualityInfo.from_document(info.extraction_quality, d.rag.analysis)
        note = (
            "The contract type was not set, so the rules for every contract and for an MSA "
            "were applied."
            if contract_type is ContractType.UNKNOWN
            else None
        )
        return {
            "document": info.model_dump(mode="json"),
            "quality": {"ok": quality.ok, "text_ratio": quality.text_ratio},
            "rule_ids": [r.id for r in rules],
            "contract_type_note": note,
        }

    async def plan_rule_batches(self, state: AnalysisState) -> dict[str, Any]:
        size = self._d.rag.analysis.rules_per_batch
        ids = state["rule_ids"]
        batches = [ids[i : i + size] for i in range(0, len(ids), size)]
        get_stream_writer()({"event": "plan", "batches": len(batches)})
        return {"batches": batches}

    def fan_out(self, state: AnalysisState) -> list[Send] | str:
        """One `assess_rule_batch` per batch, in parallel (bounded by `max_concurrency`)."""
        if state.get("error"):
            return END
        shared = {
            key: state[key]
            for key in ("job_id", "owner_id", "document_id", "document", "quality")
            if key in state
        }
        return [Send("assess_rule_batch", {**shared, "batch": batch}) for batch in state["batches"]]

    # ---- one batch of rules -----------------------------------------------------------------

    async def assess_rule_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        d = self._d
        document = DocumentInfo.model_validate(payload["document"])
        quality = ExtractionQualityInfo(**payload["quality"])
        scope = d.scope_for(UUID(payload["owner_id"]))
        rules = [d.ruleset.rule(rule_id) for rule_id in payload["batch"]]

        gathered = await asyncio.gather(
            *[self._gather(rule, document, scope) for rule in rules], return_exceptions=False
        )
        assessments, llm_error, usage = await self._ask_model(document, gathered)
        results = [
            self._decide(g, document, quality, assessments.get(g.rule.id), llm_error)
            for g in gathered
        ]
        get_stream_writer()({"event": "batch_done"})
        return {
            "rule_results": [r.model_dump(mode="json") for r in results],
            "usage": usage,
        }

    async def _gather(self, rule: RuleSpec, document: DocumentInfo, scope: Any) -> Gathered:
        """Targeted retrieval for one rule: each query, merged and deduplicated (spec 20.4)."""
        config = self._d.rag.analysis
        found = Gathered(rule)
        try:
            results = []
            for query in rule.retrieval.queries:
                results.append(
                    await self._d.retrieval.retrieve(
                        RetrievalRequest(
                            scope=scope,
                            query=query,
                            document_ids=[document.id],
                            keywords=rule.retrieval.keywords,
                            clause_type_boost=rule.clause_types,
                            k_final=config.k_final_per_query,
                        )
                    )
                )
                found.queries_run += 1
        except Exception:
            log.error("analysis.retrieval_failed", rule_id=rule.id, exc_info=True)
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
        self, document: DocumentInfo, gathered: Sequence[Gathered]
    ) -> tuple[dict[str, RuleAssessment], str | None, dict[str, int]]:
        """One structured call for the whole batch (spec 20.4 step 3). A failure here costs these
        rules their assessment, not the job: each becomes `insufficient_evidence`."""
        askable = [g for g in gathered if g.error is None]
        if not askable:
            return {}, None, {}
        blocks = []
        for g in askable:
            hits = run_heuristics(g.rule, g.sources)
            blocks.append(_rule_block(g, hits))
        prompt = (
            f'<contract title="{escape_attribute(document.title)}" '
            f'type="{escape_attribute(document.contract_type)}"/>\n\n' + "\n\n".join(blocks)
        )
        request = self._d.routes.request(
            "rule_assessment", load_prompt("rule_assessment.v1"), [ChatMessage("user", prompt)]
        )
        try:
            result = await self._d.llm.structured(request, RuleAssessmentBatch)
        except (CovenantError, ValueError) as exc:
            log.warning("analysis.batch_failed", error=type(exc).__name__)
            return {}, graph_error(exc, "assess_rule_batch").code, {}
        return (
            {a.rule_id: a for a in result.value.assessments},
            None,
            counts(result.usage),
        )

    def _decide(
        self,
        gathered: Gathered,
        document: DocumentInfo,
        quality: ExtractionQualityInfo,
        assessment: RuleAssessment | None,
        llm_error: str | None,
    ) -> RuleResult:
        rule = gathered.rule
        config = self._d.rag.analysis
        trace = {
            "queries": rule.retrieval.queries,
            "queries_run": gathered.queries_run,
            "best_rerank": gathered.best_rerank,
            "pages_scanned": document.pages_total,
            "candidates": len(gathered.sources),
            "taxonomy_clause_present": any(
                t in document.clause_types_present for t in rule.clause_types
            ),
        }
        error = gathered.error or (llm_error if assessment is None else None)
        if error is None and assessment is None:
            error = "MISSING_ASSESSMENT"
        hits = run_heuristics(rule, gathered.sources)
        observations = observe(rule, gathered.sources)

        sources = {s.marker: s for s in gathered.sources}
        cited_text = " ".join(
            sources[m].text
            for m in (assessment.supporting_markers if assessment else [])
            if m in sources
        )
        supported = bool(
            assessment
            and cited_text
            and self._d.citations.supports(assessment.explanation, cited_text)[0]
        )
        evidence = Evidence(
            sources=gathered.sources,
            best_rerank=gathered.best_rerank,
            taxonomy_clause_present=bool(trace["taxonomy_clause_present"]),
            hits=hits,
            assessment=assessment if error is None else None,
            explanation_supported=supported,
        )
        decision = classify(evidence, quality, config)
        return _result(
            rule, decision, evidence, assessment, quality, trace, hits, observations, error, self._d
        )

    # ---- after the batches ------------------------------------------------------------------

    async def aggregate_rule_results(self, state: AnalysisState) -> dict[str, Any]:
        if state.get("error"):  # a batch failed in a way no rule result can describe
            return {}
        get_stream_writer()({"event": "progress", "stage": "assessing_rules", "fraction": 0.75})
        results = state.get("rule_results", [])
        expected = state.get("rule_ids", [])
        got = {r["rule_id"] for r in results}
        missing = [rule_id for rule_id in expected if rule_id not in got]
        failed = sum(1 for r in results if r.get("error")) + len(missing)
        total = max(1, len(expected))
        if failed / total > self._d.rag.analysis.max_degraded_fraction:
            return {
                "error": _error(
                    DEGRADED,
                    f"{failed} of {total} rules could not be assessed.",
                    True,
                    "aggregate_rule_results",
                )
            }
        order = {rule_id: i for i, rule_id in enumerate(expected)}
        ordered = sorted(results, key=lambda r: order.get(r["rule_id"], 10_000))
        warnings = [f"{rule_id}: not assessed" for rule_id in missing]
        return {"validated_results": ordered, "warnings": warnings}

    async def extract_obligations(self, state: AnalysisState) -> dict[str, Any]:
        d = self._d
        get_stream_writer()({"event": "progress", "stage": "obligations", "fraction": 0.8})
        document = DocumentInfo.model_validate(state["document"])
        scope = d.scope_for(UUID(state["owner_id"]))
        config = d.rag.analysis
        try:
            results = [
                await d.retrieval.retrieve(
                    RetrievalRequest(
                        scope=scope,
                        query=query,
                        document_ids=[document.id],
                        k_final=config.k_final_per_query,
                    )
                )
                for query in obligation_rules.QUERIES
            ]
            merged = merge_results(results, config.obligation_sources)
            bundle = build_context(
                merged,
                max_tokens=config.context_tokens_per_rule * 2,
                parent_max_tokens=d.rag.context.parent_max_tokens,
            )
            by_chunk = {c.chunk_id: c.rerank_score for c in merged.chunks}
            sources = [to_source(s, by_chunk) for s in bundle.sources]
            if not sources:
                return {"obligations": []}
            request = d.routes.request(
                "signal_extraction",
                load_prompt("obligations.v1"),
                [ChatMessage("user", f"<sources>\n{bundle.text}\n</sources>")],
            )
            extracted = await d.llm.structured(request, ObligationExtraction)
        except Exception as exc:
            # Obligations are an extra: the analysis stands without them.
            log.warning("analysis.obligations_failed", error=type(exc).__name__, exc_info=True)
            return {"obligations": [], "warnings": ["obligations could not be extracted"]}
        records = obligation_rules.validate_obligations(extracted.value.obligations, sources)
        return {
            "obligations": [r.model_dump(mode="json") for r in records],
            "usage": counts(extracted.usage),
        }

    async def detect_internal_contradictions(self, state: AnalysisState) -> dict[str, Any]:
        d = self._d
        get_stream_writer()({"event": "progress", "stage": "contradictions", "fraction": 0.88})
        observations = [
            ValueObservation.model_validate(o)
            for r in state.get("validated_results", [])
            for o in r.get("observations", [])
        ]
        pairs = find_candidates(observations, limit=d.rag.analysis.max_contradiction_pairs)
        confirmed: list[dict[str, Any]] = []
        usage: dict[str, int] = {}
        warnings: list[str] = []
        for first, second in pairs:
            prompt = (
                f"<value_a>{escape_text(first.raw)}</value_a>\n"
                f'<passage_a clause="{escape_attribute(first.source.clause_ref or "")}">\n'
                f"{escape_text(first.source.text)}\n</passage_a>\n\n"
                f"<value_b>{escape_text(second.raw)}</value_b>\n"
                f'<passage_b clause="{escape_attribute(second.source.clause_ref or "")}">\n'
                f"{escape_text(second.source.text)}\n</passage_b>"
            )
            request = d.routes.request(
                "rule_assessment",
                load_prompt("contradiction_confirm.v1"),
                [ChatMessage("user", prompt)],
            )
            try:
                verdict = await d.llm.structured(request, ContradictionVerdict)
            except (CovenantError, ValueError):
                warnings.append(f"contradiction check skipped for {first.key}")
                continue
            for key, value in counts(verdict.usage).items():
                usage[key] = usage.get(key, 0) + value
            if verdict.value.is_contradiction:
                confirmed.append(
                    ContradictionRecord(
                        key=first.key,
                        first=first,
                        second=second,
                        explanation=verdict.value.explanation,
                    ).model_dump(mode="json")
                )
        return {"contradictions": confirmed, "usage": usage, "warnings": warnings}

    async def scan_unusual_provisions(self, state: AnalysisState) -> dict[str, Any]:
        d = self._d
        get_stream_writer()({"event": "progress", "stage": "unusual_provisions", "fraction": 0.92})
        chunks = await d.store.document_chunks(UUID(state["owner_id"]), UUID(state["document_id"]))
        hits: list[dict[str, Any]] = []
        for provision in d.ruleset.unusual:
            pattern = re.compile(provision.pattern)
            matched = 0
            for chunk in chunks:
                match = pattern.search(chunk.text[:8000])
                if match is None:
                    continue
                span = " ".join(chunk.text[max(0, match.start() - 30) : match.end() + 30].split())
                source = SourceRef(
                    marker=1,
                    chunk_ids=[chunk.chunk_id],
                    clause_id=chunk.clause_id,
                    document_id=chunk.document_id,
                    document_title=chunk.document_title,
                    clause_ref=chunk.clause_ref,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    text=chunk.text,
                )
                draft = finding_builders.draft_from_unusual(provision, source, span[:200], 0)
                hits.append(draft.model_dump(mode="json"))
                matched += 1
                if matched >= 3:  # three examples are plenty; the rest add noise
                    break
        return {"unusual": hits}

    async def validate_evidence(self, state: AnalysisState) -> dict[str, Any]:
        """A last check on the finished results (spec 20.4 step 5): a finding that claims a clause
        exists, or is acceptable, must stand on at least one cited passage; otherwise it is
        downgraded to `insufficient_evidence` rather than shown as fact."""
        checked: list[dict[str, Any]] = []
        warnings: list[str] = []
        for raw in state.get("validated_results", []):
            result = RuleResult.model_validate(raw)
            needs_evidence = result.status in (
                ClauseStatus.PROBLEMATIC,
                ClauseStatus.AMBIGUOUS,
                ClauseStatus.ACCEPTABLE,
            )
            if needs_evidence and not result.sources:
                warnings.append(f"{result.rule_id}: downgraded, no valid evidence")
                result = result.model_copy(
                    update={
                        "status": ClauseStatus.INSUFFICIENT_EVIDENCE,
                        "severity": Severity.INFO,
                        "evidence_sufficiency": EvidenceSufficiency.NONE,
                        "explanation": "A finding was proposed but no passage supports it.",
                        "reasons": [*result.reasons, "no valid evidence"],
                    }
                )
            checked.append(result.model_dump(mode="json"))
        return {"validated_results": checked, "warnings": warnings}

    async def build_findings(self, state: AnalysisState) -> dict[str, Any]:
        get_stream_writer()({"event": "progress", "stage": "building_findings", "fraction": 0.95})
        drafts = [
            finding_builders.draft_from_rule(RuleResult.model_validate(r))
            for r in state.get("validated_results", [])
        ]
        drafts += [
            finding_builders.draft_from_obligation(ObligationRecord.model_validate(o), i)
            for i, o in enumerate(state.get("obligations", []))
        ]
        drafts += [
            finding_builders.draft_from_contradiction(ContradictionRecord.model_validate(c), i)
            for i, c in enumerate(state.get("contradictions", []))
        ]
        unusual = [FindingDraft.model_validate(u) for u in state.get("unusual", [])]
        numbered = finding_builders.number_ordinals([*drafts, *unusual])
        stats: dict[str, Any] = {
            **finding_builders.summarise(numbered),
            "usage": state.get("usage", {}),
        }
        notes = [n for n in (state.get("contract_type_note"),) if n]
        if notes or state.get("warnings"):
            stats["notes"] = [*notes, *state.get("warnings", [])]
        return {"findings": [f.model_dump(mode="json") for f in numbered], "stats": stats}

    async def persist_findings(self, state: AnalysisState) -> dict[str, Any]:
        drafts = [FindingDraft.model_validate(f) for f in state.get("findings", [])]
        await self._d.store.persist(
            UUID(state["owner_id"]),
            UUID(state["job_id"]),
            UUID(state["document_id"]),
            drafts,
            state.get("stats", {}),
        )
        get_stream_writer()({"event": "progress", "stage": "done", "fraction": 1.0})
        return {}


# ---- helpers ------------------------------------------------------------------------------------


def _error(code: str, message: str, retryable: bool, node: str) -> dict[str, Any]:
    return {"code": code, "message": message, "retryable": retryable, "node": node}


def _rule_block(gathered: Gathered, hits: Sequence[HeuristicHit]) -> str:
    rule = gathered.rule
    questions = "\n".join(f"- {q}" for q in rule.assessment_questions)
    values = "\n".join(f"- {name}: {hint}" for name, hint in rule.extract.items()) or "- (none)"
    matches = "\n".join(
        f'<pattern_match id="{escape_attribute(h.heuristic_id)}" source="C{h.marker}">'
        f"{escape_text(h.span)}</pattern_match>"
        for h in hits
    )
    sources = gathered.context_text or "(no passages were found)"
    return (
        f'<rule id="{rule.id}" title="{escape_attribute(rule.title)}">\n'
        f"<questions>\n{questions}\n</questions>\n"
        f"<values_to_extract>\n{values}\n</values_to_extract>\n"
        + (f"{matches}\n" if matches else "")
        + f"<sources>\n{sources}\n</sources>\n</rule>"
    )


def _result(
    rule: RuleSpec,
    decision: Classification,
    evidence: Evidence,
    assessment: RuleAssessment | None,
    quality: ExtractionQualityInfo,
    trace: dict[str, Any],
    hits: Sequence[HeuristicHit],
    observations: list[ValueObservation],
    error: str | None,
    deps: AnalysisDeps,
) -> RuleResult:
    status = decision.status
    sources = {s.marker: s for s in evidence.sources}
    cited = [sources[m] for m in decision.markers if m in sources]
    usable_assessment = assessment if error is None else None
    severity = severity_for(rule, decision, hits)
    confidence = confidence_for(usable_assessment, evidence.best_rerank, quality)
    sufficiency = (
        sufficiency_for(
            decision.markers,
            evidence.sources,
            claimed=len(assessment.supporting_markers) if assessment else 0,
        )
        if status is not ClauseStatus.ABSENT
        else EvidenceSufficiency.NONE
    )
    explanation, impact, action = _words(rule, decision, usable_assessment, evidence, trace, hits)
    first = cited[0] if cited else None
    values = (
        {v.name: v.value for v in usable_assessment.extracted_values} if usable_assessment else {}
    )
    return RuleResult(
        rule_id=rule.id,
        title=rule.title,
        category=rule.category,
        status=status,
        severity=severity,
        confidence=confidence,
        evidence_sufficiency=sufficiency,
        explanation=explanation,
        business_impact=impact,
        recommended_action=action,
        clause_ref=first.clause_ref if first else None,
        page_number=first.page_start if first else None,
        markers=decision.markers,
        sources=cited,
        red_flags=list(usable_assessment.red_flags) if usable_assessment else [],
        heuristic_hits=list(hits),
        extracted_values=values,
        observations=observations,
        reasons=decision.reasons,
        search_trace=trace,
        error=error,
    )


def _words(
    rule: RuleSpec,
    decision: Classification,
    assessment: RuleAssessment | None,
    evidence: Evidence,
    trace: dict[str, Any],
    hits: Sequence[HeuristicHit],
) -> tuple[str, str | None, str | None]:
    """The text of a finding. Absence and could-not-assess are written from facts; the others use
    the model's explanation, which has been checked against the passages it cites."""
    actions = rule.recommended_actions
    status = decision.status
    if status is ClauseStatus.ABSENT:
        return (
            finding_builders.absent_explanation(rule.title, trace),
            finding_builders.clip(assessment.business_impact, 800) if assessment else None,
            actions.get("absent"),
        )
    if status is ClauseStatus.INSUFFICIENT_EVIDENCE:
        why = "; ".join(decision.reasons) or "the evidence was not sufficient"
        return (
            f"This could not be assessed automatically: {why}. Review it manually.",
            None,
            "Review this clause manually.",
        )
    forced = [h for h in hits if h.status == "problematic"]
    if assessment is not None and evidence.explanation_supported:
        explanation = assessment.explanation
    elif forced:
        explanation = f"A pattern for “{forced[0].heuristic_id}” matched: “{forced[0].span}”."
    else:
        explanation = assessment.explanation if assessment else EXPLANATION_FALLBACK
    impact = assessment.business_impact if assessment and assessment.business_impact else None
    if status is ClauseStatus.ACCEPTABLE:
        return explanation, impact, None
    key: Literal["problematic", "ambiguous"] = (
        "problematic" if status is ClauseStatus.PROBLEMATIC else "ambiguous"
    )
    action = (
        assessment.recommended_action if assessment and assessment.recommended_action else None
    ) or actions.get(key)
    return explanation, impact, action
