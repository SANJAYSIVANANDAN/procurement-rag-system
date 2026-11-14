"""The reply when the evidence is not good enough: a template, no model call (spec 19.10)."""

from collections.abc import Sequence

from covenantiq.rag.types import EvidenceVerdict

ABSTENTION_HEADLINE = "I couldn't find enough in the selected documents to answer that reliably."
UNVERIFIED_NOTICE = "Some statements could not be verified against the documents."


def abstention_message(
    document_titles: Sequence[str], verdict: EvidenceVerdict | None = None
) -> str:
    """Says what was searched and what to try next. Deterministic, so it can be tested and never
    invents an answer."""
    lines = [ABSTENTION_HEADLINE]
    if document_titles:
        shown = ", ".join(f'"{title}"' for title in document_titles[:5])
        extra = len(document_titles) - 5
        lines.append(f"I searched: {shown}" + (f" and {extra} more." if extra > 0 else "."))
    else:
        lines.append("There were no ready documents to search.")
    if verdict is not None and verdict.reasons:
        lines.append("Why: " + "; ".join(verdict.reasons) + ".")
    lines.append(
        "You could rephrase the question with the terms the contract would use, name a clause "
        "or schedule, or select other documents."
    )
    return "\n\n".join(lines)
