"""Checking an answer against the sources it cites (spec 19.12).

Fully deterministic and offline: no model call. The answer is split into sentences; every `[C#]`
marker must name a real source; every sentence with a marker must be supported by what it cites; a
sentence that states a number, date or amount with no marker is flagged. The result decides whether
the answer is shown as it is, shown with a notice, or replaced by the abstention template.

Beyond the spec: a digit in a cited sentence ("30 days") must also appear in its source, since a
sentence can share most of its words with a source and still change the number.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from rapidfuzz import fuzz

from covenantiq.ingestion.chunking.sentence_splitter import split_sentences
from covenantiq.rag.abstention import UNVERIFIED_NOTICE, abstention_message
from covenantiq.rag.config import CitationConfig
from covenantiq.rag.evidence import stems
from covenantiq.rag.types import ContextSource

# [C1]  [C1, C3]  [C1,C3]; adjacent markers like [C1][C3] are simply two matches
_MARKER = re.compile(r"\[(C\d+(?:\s*,\s*C\d+)*)\]")
_MARKER_AFTER_STOP = re.compile(r"([.!?])(\s*)((?:\[C\d+(?:\s*,\s*C\d+)*\]\s*)+)")
_QUOTED = re.compile(r"[\"“]([^\"”]{4,})[\"”]")
_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
_MONEY = re.compile(r"[$€£]\s?\d")
_MONTH = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b"
)
_NUMBER_WORD = re.compile(
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty"
    r"|forty|fifty|sixty|ninety|hundred|thousand|million)\b",
    re.IGNORECASE,
)
_DURATION = re.compile(r"\b(?:days?|weeks?|months?|years?)\b", re.IGNORECASE)
# a defined term such as "Confidential Information", away from the start of a sentence
_DEFINED_TERM = re.compile(r"(?<=[a-z,;:] )[A-Z][a-z]+(?: [A-Z][a-z]+)+\b")


@dataclass
class SentenceCheck:
    text: str
    markers: list[str]  # valid markers only
    supported: bool | None  # None: the sentence carries no valid marker
    unmarked_claim: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class Citation:
    marker: str
    quote: str  # what to show next to the answer: a sentence of the source
    document_id: UUID
    clause_id: UUID
    clause_ref: str | None
    page_start: int | None
    page_end: int | None
    chunk_ids: tuple[UUID, ...]


@dataclass
class ValidationResult:
    text: str  # the answer to show: unknown markers removed, a notice added, or the template
    status: str  # grounded | partially_grounded | ungrounded | abstained
    citations: list[Citation] = field(default_factory=list)
    invalid_markers: int = 0
    unsupported: int = 0
    unmarked_claims: int = 0
    sentences: list[SentenceCheck] = field(default_factory=list)
    discarded_answer_sha: str | None = None  # only the hash of a replaced answer is kept


def _markers(text: str) -> list[str]:
    return [m.strip() for match in _MARKER.finditer(text) for m in match.group(1).split(",")]


def _strip_markers(text: str) -> str:
    return " ".join(_MARKER.sub(" ", text).split())


def _state_a_fact(sentence: str) -> bool:
    """Does this sentence assert something checkable: a number, date, amount, duration or a
    defined term? Only such sentences need a citation."""
    return bool(
        _DIGITS.search(sentence)
        or _MONEY.search(sentence)
        or _MONTH.search(sentence)
        or (_NUMBER_WORD.search(sentence) and _DURATION.search(sentence))
        or _DEFINED_TERM.search(sentence)
    )


class CitationValidator:
    def __init__(self, config: CitationConfig | None = None) -> None:
        self._config = config or CitationConfig()

    def validate(
        self,
        answer: str,
        marker_map: Mapping[str, ContextSource],
        *,
        document_titles: list[str] | None = None,
    ) -> ValidationResult:
        known = set(marker_map)
        invalid = [m for m in _markers(answer) if m not in known]
        cleaned = self._remove_unknown_markers(answer, known)

        # "...months. [C1]" means the sentence before it: move such markers inside the sentence
        normalised = _MARKER_AFTER_STOP.sub(lambda m: f" {m.group(3).strip()}{m.group(1)}", cleaned)
        checks = [
            self._check(normalised[start:end], marker_map)
            for start, end in split_sentences(normalised)
            if _strip_markers(normalised[start:end])
        ]

        used = sorted(
            {marker for check in checks for marker in check.markers}, key=lambda m: int(m[1:])
        )
        unsupported = sum(1 for check in checks if check.supported is False)
        unmarked = sum(1 for check in checks if check.unmarked_claim)

        if not used:
            return ValidationResult(
                text=abstention_message(document_titles or []),
                status="abstained",
                invalid_markers=len(invalid),
                unsupported=unsupported,
                unmarked_claims=unmarked,
                sentences=checks,
                discarded_answer_sha=hashlib.sha256(answer.encode()).hexdigest(),
            )

        factual = sum(1 for c in checks if c.markers or c.unmarked_claim)
        bad = unsupported + unmarked
        if bad == 0:
            status, text = "grounded", cleaned
        elif bad / factual <= self._config.partial_unsupported_fraction:
            status, text = "partially_grounded", cleaned
        else:
            status, text = "ungrounded", f"{cleaned.rstrip()}\n\n{UNVERIFIED_NOTICE}"
        return ValidationResult(
            text=text,
            status=status,
            citations=[self._citation(m, marker_map[m], checks) for m in used],
            invalid_markers=len(invalid),
            unsupported=unsupported,
            unmarked_claims=unmarked,
            sentences=checks,
        )

    @staticmethod
    def _remove_unknown_markers(answer: str, known: set[str]) -> str:
        def keep(match: re.Match[str]) -> str:
            wanted = [m.strip() for m in match.group(1).split(",")]
            valid = [m for m in wanted if m in known]
            if not valid:
                return ""
            return f"[{', '.join(valid)}]" if len(valid) != len(wanted) else match.group(0)

        text = _MARKER.sub(keep, answer)
        return re.sub(r"[ \t]+([.,;:!?])", r"\1", re.sub(r"[ \t]{2,}", " ", text))

    def _check(self, sentence: str, marker_map: Mapping[str, ContextSource]) -> SentenceCheck:
        markers = [m for m in dict.fromkeys(_markers(sentence)) if m in marker_map]
        stripped = _strip_markers(sentence)
        if not markers:
            return SentenceCheck(
                stripped,
                [],
                None,
                unmarked_claim=_state_a_fact(stripped),
                reason="states a fact without a citation" if _state_a_fact(stripped) else None,
            )
        source_text = " ".join(marker_map[m].text for m in markers)
        supported, reason = self._supported(stripped, source_text)
        return SentenceCheck(stripped, markers, supported, reason=reason)

    def _supported(self, sentence: str, source_text: str) -> tuple[bool, str | None]:
        config = self._config
        quotes = [q.strip() for q in _QUOTED.findall(sentence)]
        if quotes:
            for quote in quotes:  # one fabricated quote sinks the sentence, whatever else matches
                if (
                    fuzz.partial_ratio(quote.lower(), source_text.lower())
                    < config.quote_fuzzy_ratio
                ):
                    return False, f"the quotation {quote[:60]!r} is not in the cited source"
            return True, None
        wanted = stems(sentence)
        if not wanted:
            return False, "nothing in the sentence can be checked against the source"
        overlap = len(wanted & stems(source_text)) / len(wanted)
        if overlap < config.overlap_threshold:
            return False, f"only {overlap:.0%} of its content words are in the cited source"
        source_numbers = set(_DIGITS.findall(source_text))
        wrong = [n for n in _DIGITS.findall(sentence) if n not in source_numbers]
        if wrong:
            return False, f"the number {wrong[0]} is not in the cited source"
        return True, None

    def supports(self, statement: str, source_text: str) -> tuple[bool, str | None]:
        """Is `statement` supported by `source_text`? The check chat answers use, for callers
        (contract analysis) that cite sources without `[C#]` markers in running text."""
        return self._supported(statement, source_text)

    def quote_for(self, source_text: str, claims: list[str]) -> str:
        """The sentence of a source that best matches what was claimed about it."""
        return self._best_quote(source_text, claims)

    def _citation(
        self, marker: str, source: ContextSource, checks: list[SentenceCheck]
    ) -> Citation:
        return Citation(
            marker=marker,
            quote=self._best_quote(source.text, [c.text for c in checks if marker in c.markers]),
            document_id=source.document_id,
            clause_id=source.clause_id,
            clause_ref=source.clause_ref,
            page_start=source.page_start,
            page_end=source.page_end,
            chunk_ids=source.chunk_ids,
        )

    def _best_quote(self, source_text: str, claims: list[str]) -> str:
        """The sentence of the source that best matches what the answer said about it, short enough
        to show beside the answer."""
        limit = self._config.quote_max_chars
        claim_stems = stems(" ".join(claims))
        best, best_score = "", -1.0
        for start, end in split_sentences(source_text):
            sentence = source_text[start:end]
            score = len(claim_stems & stems(sentence)) / max(1, len(stems(sentence)) ** 0.5)
            if score > best_score:
                best, best_score = sentence, score
        if len(best) <= limit:
            return best
        cut = best[:limit].rsplit(" ", 1)[0]
        return f"{cut}…"
