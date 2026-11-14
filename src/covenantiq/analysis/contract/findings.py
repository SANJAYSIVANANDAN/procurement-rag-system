"""From analysis results to findings (spec 20.8, 20.9).

Every finding carries the evidence it stands on. Statements that need no model (an absence, a
could-not-assess, an unusual provision) are written here from facts, so they cannot be made up.
"""

import re
from collections import Counter
from collections.abc import Sequence

from covenantiq.analysis.contract.rules_schema import UnusualProvision
from covenantiq.analysis.contract.types import (
    ContradictionRecord,
    FindingDraft,
    ObligationRecord,
    RuleResult,
    SourceRef,
)
from covenantiq.core.enums import ClauseStatus, EvidenceSufficiency, RiskCategory, Severity

DISCLAIMER = (
    "Automated analysis for decision support. Not legal advice. Verify with qualified counsel."
)
MAX_EXPLANATION = 1200
MAX_IMPACT = 800
MAX_ACTION = 600


def clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _title(result: RuleResult) -> str:
    base = result.title
    return {
        ClauseStatus.ABSENT: f"No {base[:1].lower()}{base[1:]} found",
        ClauseStatus.PROBLEMATIC: f"{base}: terms that create risk",
        ClauseStatus.AMBIGUOUS: f"{base}: unclear or incomplete",
        ClauseStatus.INSUFFICIENT_EVIDENCE: f"{base}: could not be assessed",
        ClauseStatus.ACCEPTABLE: f"{base}: within baseline",
    }[result.status]


def draft_from_rule(result: RuleResult, ordinal: int = 0) -> FindingDraft:
    details = {
        "kind": "rule",
        "red_flags": result.red_flags,
        "heuristic_hits": [h.model_dump(mode="json") for h in result.heuristic_hits],
        "extracted_values": result.extracted_values,
        "reasons": result.reasons,
        "search_trace": result.search_trace,
    }
    if result.error:
        details["error"] = result.error
    return FindingDraft(
        rule_id=result.rule_id,
        ordinal=ordinal,
        risk_category=result.category,
        clause_status=result.status,
        severity=result.severity,
        title=_title(result),
        explanation=clip(result.explanation, MAX_EXPLANATION) or "",
        business_impact=clip(result.business_impact, MAX_IMPACT),
        recommended_action=clip(result.recommended_action, MAX_ACTION),
        clause_ref=result.clause_ref,
        page_number=result.page_number,
        confidence=result.confidence,
        evidence_sufficiency=result.evidence_sufficiency,
        evidence=list(result.sources),
        details=details,
    )


def absent_explanation(title: str, trace: dict[str, object]) -> str:
    pages = trace.get("pages_scanned")
    best = trace.get("best_rerank")
    parts = [f"No {title.lower()} was found in the document."]
    searched = f"{trace.get('queries_run', 0)} targeted searches"
    if pages:
        searched += f" across {pages} pages"
    parts.append(f"It was searched for with {searched}")
    if isinstance(best, int | float):
        parts[-1] += f"; the closest passage scored {best:.2f}."
    else:
        parts[-1] += "."
    return " ".join(parts)


def draft_from_obligation(obligation: ObligationRecord, ordinal: int) -> FindingDraft:
    deadline = obligation.deadline_text or "no stated deadline"
    return FindingDraft(
        rule_id=f"OBL-{ordinal + 1:03d}",
        ordinal=ordinal,
        risk_category=RiskCategory.OBLIGATIONS,
        clause_status=ClauseStatus.ACCEPTABLE,  # not a judgement: obligations are listed, not rated
        severity=Severity.INFO,
        title=f"{obligation.party.capitalize()}: {clip(obligation.action, 120)}",
        explanation=f"{obligation.party.capitalize()} must {obligation.action} ({deadline}).",
        clause_ref=obligation.clause_ref,
        page_number=obligation.page_number,
        confidence=0.6,
        evidence_sufficiency=EvidenceSufficiency.MODERATE,
        evidence=[obligation.source],
        details={
            "kind": "obligation",
            "party": obligation.party,
            "action": obligation.action,
            "deadline_text": obligation.deadline_text,
            "deadline_days": obligation.deadline_days,
            "trigger": obligation.trigger,
        },
    )


def draft_from_contradiction(item: ContradictionRecord, ordinal: int) -> FindingDraft:
    first, second = item.first, item.second
    label = item.key.replace("_", " ")
    return FindingDraft(
        rule_id=f"CONTRA-{re.sub('[^A-Z0-9]+', '-', item.key.upper()).strip('-')}",
        ordinal=ordinal,
        risk_category=RiskCategory.GENERAL,
        clause_status=ClauseStatus.PROBLEMATIC,
        severity=Severity.MEDIUM,
        title=f"Conflicting {label} in the document",
        explanation=clip(item.explanation, MAX_EXPLANATION) or "",
        recommended_action=f"Ask legal to confirm which {label} applies and align the clauses.",
        clause_ref=first.source.clause_ref,
        page_number=first.source.page_start,
        confidence=0.6,
        evidence_sufficiency=EvidenceSufficiency.STRONG,
        evidence=[first.source, second.source],
        details={
            "kind": "contradiction",
            "key": item.key,
            "values": [first.raw, second.raw],
            "rules": sorted({first.rule_id, second.rule_id}),
        },
    )


def draft_from_unusual(
    provision: UnusualProvision, source: SourceRef, span: str, ordinal: int
) -> FindingDraft:
    return FindingDraft(
        rule_id=provision.rule_id,
        ordinal=ordinal,
        risk_category=provision.category,
        clause_status=ClauseStatus.PROBLEMATIC,
        severity=provision.severity,
        title=provision.title,
        explanation=f"{provision.explanation} Matched text: “{span}”.",
        recommended_action=provision.recommended_action,
        clause_ref=source.clause_ref,
        page_number=source.page_start,
        confidence=0.7,  # a pattern match is precise about what it matched, not about its effect
        evidence_sufficiency=EvidenceSufficiency.MODERATE,
        evidence=[source],
        details={"kind": "unusual", "pattern_id": provision.id},
    )


def number_ordinals(drafts: Sequence[FindingDraft]) -> list[FindingDraft]:
    """Give findings of one rule consecutive ordinals (the table is keyed job, rule, ordinal)."""
    seen: Counter[str] = Counter()
    numbered = []
    for draft in drafts:
        numbered.append(draft.model_copy(update={"ordinal": seen[draft.rule_id]}))
        seen[draft.rule_id] += 1
    return numbered


def summarise(drafts: Sequence[FindingDraft]) -> dict[str, dict[str, int]]:
    """Counts by severity and by status, for `analysis_jobs.params.stats`."""
    return {
        "by_severity": dict(Counter(d.severity.value for d in drafts)),
        "by_status": dict(Counter(d.clause_status.value for d in drafts)),
    }
