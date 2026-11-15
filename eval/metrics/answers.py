"""Metrics on a chat answer: its citations, its claims, and whether it should have been given."""

from collections.abc import Sequence
from dataclasses import dataclass

from eval.dataset import Passage
from eval.metrics.stats import Ratio
from eval.metrics.text import OVERLAP_THRESHOLD, overlaps


@dataclass(frozen=True)
class EmittedCitation:
    """A citation the answer carried: where it points, and the text of the passage it cites."""

    document: str
    page_start: int | None
    page_end: int | None
    text: str


def _pages_overlap(citation: EmittedCitation, expected: Passage) -> bool:
    """Page ranges intersect; if either side has no page, the page cannot rule it out."""
    if citation.page_start is None or expected.page is None:
        return True
    end = citation.page_end if citation.page_end is not None else citation.page_start
    return citation.page_start <= expected.page <= end


def citation_correctness(
    citations: Sequence[EmittedCitation], expected: Sequence[Passage]
) -> Ratio:
    """The share of citations whose passage overlaps an expected one: the same document, an
    overlapping page, and text overlap of at least 0.5 (token Jaccard). Spec 28.3."""
    if not citations:
        return 0.0, 0.0
    correct = sum(
        1
        for c in citations
        if any(
            c.document == e.document
            and _pages_overlap(c, e)
            and overlaps(c.text, e.text, OVERLAP_THRESHOLD)
            for e in expected
        )
    )
    return float(correct), float(len(citations))


def citation_validity(markers_total: int, markers_invalid: int) -> Ratio:
    """The share of citation markers in the draft that named a real source."""
    return float(max(0, markers_total - markers_invalid)), float(markers_total)


def unsupported_claim_rate(unsupported: int, unmarked: int, factual: int) -> Ratio:
    """(unsupported + unmarked factual sentences) / factual sentences. Lower is better."""
    return float(unsupported + unmarked), float(factual)


@dataclass(frozen=True)
class AbstentionOutcome:
    answerable: bool
    abstained: bool

    @property
    def correct(self) -> bool:
        return self.abstained != self.answerable  # answered what is answerable, abstained otherwise

    @property
    def false_abstention(self) -> bool:
        return self.answerable and self.abstained

    @property
    def false_answer(self) -> bool:
        return not self.answerable and not self.abstained


def abstention_accuracy(outcomes: Sequence[AbstentionOutcome]) -> list[Ratio]:
    return [(1.0 if o.correct else 0.0, 1.0) for o in outcomes]


def false_abstention_rate(outcomes: Sequence[AbstentionOutcome]) -> list[Ratio]:
    """Over answerable questions: how often the system declined to answer."""
    return [(1.0 if o.false_abstention else 0.0, 1.0) for o in outcomes if o.answerable]


def false_answer_rate(outcomes: Sequence[AbstentionOutcome]) -> list[Ratio]:
    """Over unanswerable questions: how often it answered anyway."""
    return [(1.0 if o.false_answer else 0.0, 1.0) for o in outcomes if not o.answerable]
