"""Deciding a clause's status, severity and confidence (spec 20.5, 20.7). Pure: no model, no I/O.

The model proposes; this decides. The principle behind it: *absence is a claim that needs evidence
of search*. A clause is called `absent` only when the document's text was fully extracted, the
searches found nothing close, no clause tagged with a matching type exists, and no pattern hints
that the clause is there. Anything short of that is `insufficient_evidence`, which is an honest
"could not tell", never a guess.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from covenantiq.ai.schemas import RuleAssessment
from covenantiq.analysis.contract.heuristics import forced_problem
from covenantiq.analysis.contract.rules_schema import SEVERITY_ORDER, RuleSpec
from covenantiq.analysis.contract.types import HeuristicHit, SourceRef
from covenantiq.core.enums import ClauseStatus, EvidenceSufficiency, Severity
from covenantiq.rag.config import AnalysisConfig

LLM_CONFIDENCE = {"high": 0.85, "medium": 0.6, "low": 0.35}
STRONG_RERANK = 0.7


@dataclass(frozen=True)
class ExtractionQualityInfo:
    ok: bool  # complete enough to claim that something is missing
    text_ratio: float  # share of pages with text (1.0 when the page count is unknown)

    @classmethod
    def from_document(
        cls, quality: Mapping[str, object], config: AnalysisConfig
    ) -> "ExtractionQualityInfo":
        total = quality.get("pages_total")
        with_text = quality.get("pages_with_text")
        if isinstance(total, int) and total > 0 and isinstance(with_text, int):
            ratio = min(1.0, with_text / total)
        else:
            ratio = 1.0
        insufficient = bool(quality.get("insufficient_text"))
        return cls(ok=not insufficient and ratio >= config.pages_with_text_ratio, text_ratio=ratio)


@dataclass(frozen=True)
class Evidence:
    """What retrieval and the model produced for one rule, before anything is decided."""

    sources: Sequence[SourceRef]
    best_rerank: float | None  # None: reranking was skipped, so there is no score to compare
    taxonomy_clause_present: bool  # the document has a clause tagged with one of the rule's types
    hits: Sequence[HeuristicHit]
    assessment: RuleAssessment | None
    explanation_supported: bool


@dataclass
class Classification:
    status: ClauseStatus
    reasons: list[str] = field(default_factory=list)
    markers: list[int] = field(default_factory=list)  # validated, in order
    forced: bool = False  # a heuristic decided it
    flags: list[str] = field(default_factory=list)


def validated_markers(markers: Sequence[int], sources: Sequence[SourceRef]) -> list[int]:
    """The markers that name a source given for this rule, once each, in order."""
    known = {s.marker for s in sources}
    return [m for m in dict.fromkeys(markers) if m in known]


def _insufficient(reason: str, *more: str) -> Classification:
    return Classification(ClauseStatus.INSUFFICIENT_EVIDENCE, [reason, *more])


def classify(
    evidence: Evidence, quality: ExtractionQualityInfo, config: AnalysisConfig
) -> Classification:
    """The five-way decision of spec 20.5."""
    assessment = evidence.assessment
    if assessment is None:
        return _insufficient("the assessment could not be made")
    if not quality.ok:
        return _insufficient(
            "the document's text was not extracted completely, so neither the presence nor the "
            "absence of this clause can be established"
        )

    forced = forced_problem(evidence.hits)
    flags = sorted({h.flag for h in evidence.hits if h.flag})
    hit_markers = [h.marker for h in evidence.hits if h.marker is not None]
    markers = validated_markers(assessment.supporting_markers, evidence.sources)
    present = assessment.clause_found == "yes" or bool(evidence.hits)

    if not present:
        if assessment.clause_found == "unclear":
            return _insufficient(
                "candidate passages exist but it is unclear whether they are the clause"
            )
        return _absent_or_unknown(evidence, assessment, config)

    # the clause is there: it needs evidence, and the model has to be sure enough
    usable = markers or validated_markers(hit_markers, evidence.sources)
    if not usable:
        return _insufficient("the model's citations do not match the passages it was given")
    if forced:
        return Classification(
            ClauseStatus.PROBLEMATIC,
            [f"pattern {forced[0].heuristic_id} matched"],
            markers=usable,
            forced=True,
            flags=flags,
        )
    if assessment.confidence == "low":
        return _insufficient("the model was not confident enough to decide")
    if not evidence.explanation_supported:
        return _insufficient("the explanation is not supported by the passages it cites")

    if assessment.status == "problematic":
        return Classification(ClauseStatus.PROBLEMATIC, markers=usable, flags=flags)
    if assessment.status == "ambiguous":
        return Classification(ClauseStatus.AMBIGUOUS, markers=usable, flags=flags)
    if assessment.status == "acceptable":
        if assessment.red_flags or flags:
            return Classification(
                ClauseStatus.AMBIGUOUS,
                ["rated acceptable, but red flags were raised"],
                markers=usable,
                flags=flags,
            )
        return Classification(ClauseStatus.ACCEPTABLE, markers=usable)
    return _insufficient("the model's status contradicts having found the clause")


def _absent_or_unknown(
    evidence: Evidence, assessment: RuleAssessment, config: AnalysisConfig
) -> Classification:
    """Absence needs (2) a weak search or a confident 'not found', and (3) no tagged clause."""
    low_score = evidence.best_rerank is not None and evidence.best_rerank < config.absent_threshold
    confident_no = assessment.clause_found == "no" and assessment.confidence != "low"
    if not (low_score or confident_no):
        return _insufficient("nothing was found, but the search was not conclusive")
    if evidence.taxonomy_clause_present:
        return _insufficient(
            "the document has a clause of a matching type that the search did not surface"
        )
    reasons = []
    if low_score:
        reasons.append(f"best search score {evidence.best_rerank:.2f} is below the threshold")
    if confident_no:
        reasons.append("the model found no such clause in the passages searched")
    return Classification(ClauseStatus.ABSENT, reasons)


# ---- severity -------------------------------------------------------------------------------


def raise_to(current: Severity, candidate: Severity | None) -> Severity:
    """Heuristics can raise a severity, never lower it."""
    if candidate is None:
        return current
    return max(current, candidate, key=SEVERITY_ORDER.index)


def severity_for(
    rule: RuleSpec, classification: Classification, hits: Sequence[HeuristicHit]
) -> Severity:
    policy = rule.severity_policy
    base = {
        ClauseStatus.ABSENT: policy.absent,
        ClauseStatus.PROBLEMATIC: policy.problematic_default,
        ClauseStatus.AMBIGUOUS: policy.ambiguous,
        ClauseStatus.ACCEPTABLE: Severity.INFO,
        ClauseStatus.INSUFFICIENT_EVIDENCE: Severity.INFO,
    }[classification.status]
    if classification.status in (ClauseStatus.PROBLEMATIC, ClauseStatus.AMBIGUOUS):
        for hit in hits:
            base = raise_to(base, hit.severity)
    return base


# ---- confidence and sufficiency -------------------------------------------------------------


def confidence_for(
    assessment: RuleAssessment | None,
    best_rerank: float | None,
    quality: ExtractionQualityInfo,
) -> float:
    """0.5 * model confidence + 0.3 * evidence strength + 0.2 * extraction quality (spec 20.7)."""
    llm = LLM_CONFIDENCE.get(assessment.confidence, 0.0) if assessment is not None else 0.0
    strength = min(1.0, max(0.0, best_rerank)) if best_rerank is not None else 0.0
    return round(0.5 * llm + 0.3 * strength + 0.2 * quality.text_ratio, 3)


def sufficiency_for(
    markers: Sequence[int],
    sources: Sequence[SourceRef],
    *,
    claimed: int,
) -> EvidenceSufficiency:
    """strong: two validated markers, or one scoring 0.7 or more; moderate: one; weak: the model
    cited something but none of it held up; none: nothing was cited."""
    if markers:
        by_marker = {s.marker: s for s in sources}
        top = max((by_marker[m].rerank_score or 0.0) for m in markers)
        if len(markers) >= 2 or top >= STRONG_RERANK:
            return EvidenceSufficiency.STRONG
        return EvidenceSufficiency.MODERATE
    return EvidenceSufficiency.WEAK if claimed else EvidenceSufficiency.NONE
