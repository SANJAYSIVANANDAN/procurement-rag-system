"""Sentence splitting for legal text (spec 18.6 step 3).

Returns character spans into the original text rather than new strings, so a chunk made from
consecutive sentences is always an exact substring of the clause it came from. A general-purpose
splitter breaks legal prose in the wrong places: "Sec. 4.2", "i.e. the Customer", "No. 5" and
clause numbers all end in a full stop followed by a capital or a digit.
"""

import re

from covenantiq.ingestion.chunking.tokens import CHARS_PER_TOKEN

Span = tuple[int, int]

# Words that end in a full stop without ending the sentence.
ABBREVIATIONS = frozenset(
    {
        "sec", "secs", "no", "nos", "art", "arts", "cl", "cls", "para", "paras", "sch", "app",
        "ex", "fig", "ref", "e.g", "i.e", "vs", "v", "viz", "cf", "al", "approx", "dept", "est",
        "inc", "ltd", "corp", "co", "plc", "llc", "llp", "lp", "mr", "mrs", "ms", "dr", "prof",
        "st", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    }
)  # fmt: skip

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
_CANDIDATE = re.compile(r"[.!?]+[\"')\]\u201d\u2019]*(\s+)")
_LAST_TOKEN = re.compile(r"(\S+)$")
_NUMBERING = re.compile(r"^\(?[\dIVXLCivxlc]{1,4}(?:\.[\dA-Za-z]{1,4})*$")
_SENTENCE_START = re.compile(r"[A-Z0-9(\[\"'\u201c\u2018\-\u2022*]")


def _paragraph_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    position = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        spans.append((position, match.start()))
        position = match.end()
    spans.append((position, len(text)))
    return [span for span in spans if text[span[0] : span[1]].strip()]


def _is_boundary(text: str, match: re.Match[str]) -> bool:
    after = match.end()
    if after >= len(text) or not _SENTENCE_START.match(text[after]):
        return False  # a lower-case continuation: "e.g. the", "Co. and its"
    if text[match.start()] != ".":
        return True  # ! and ? always end a sentence
    token_match = _LAST_TOKEN.search(text[: match.start()])
    if token_match is None:
        return True
    token = token_match.group(1).strip("\"'([\u201c")
    if token.lower() in ABBREVIATIONS:
        return False
    if len(token) == 1 and token.isalpha():
        return False  # an initial: "J. Smith"
    opens_the_paragraph = not text[: token_match.start(1)].strip()
    if opens_the_paragraph and _NUMBERING.match(token):
        return False  # "6." or "12.3." labelling a numbered line, not ending a sentence
    return True


def _trim(text: str, start: int, end: int) -> Span | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if end > start else None


def split_sentences(text: str) -> list[Span]:
    """Spans of the sentences in `text`, in order, without the whitespace between them."""
    spans: list[Span] = []
    for p_start, p_end in _paragraph_spans(text):
        paragraph = text[p_start:p_end]
        cursor = 0
        for match in _CANDIDATE.finditer(paragraph):
            if _is_boundary(paragraph, match):
                span = _trim(text, p_start + cursor, p_start + match.start(1))
                if span:
                    spans.append(span)
                cursor = match.end(1)
        span = _trim(text, p_start + cursor, p_end)
        if span:
            spans.append(span)
    return spans


def split_long_span(text: str, span: Span, max_tokens: int) -> list[Span]:
    """Break a span longer than `max_tokens` into pieces that fit.

    Used only for a single sentence that is itself too long (a run-on clause). Cuts are made at
    the weakest available boundary inside the window: a semicolon, then a comma, then a space.
    """
    start, end = span
    max_chars = max_tokens * CHARS_PER_TOKEN
    pieces: list[Span] = []
    while end - start > max_chars:
        window = text[start : start + max_chars]
        cut = -1
        for separator in ("; ", ", ", " "):
            found = window.rfind(separator)
            if found > max_chars // 4:
                cut = found + len(separator)
                break
        if cut < 0:
            cut = max_chars  # no separator at all: a hard cut is all that is left
        piece = _trim(text, start, start + cut)
        if piece:
            pieces.append(piece)
        start += cut
    tail = _trim(text, start, end)
    if tail:
        pieces.append(tail)
    return pieces
