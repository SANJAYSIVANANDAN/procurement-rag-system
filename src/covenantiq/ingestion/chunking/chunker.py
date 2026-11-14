"""Chunking: the child units that are embedded, searched and cited (spec 18.6 step 3)."""

from dataclasses import dataclass

from covenantiq.ingestion.chunking.clause_builder import ClauseDraft
from covenantiq.ingestion.chunking.sentence_splitter import Span, split_long_span, split_sentences
from covenantiq.ingestion.chunking.tokens import estimate_tokens
from covenantiq.ingestion.extraction.types import ExtractedBlock


@dataclass(frozen=True)
class ChunkSettings:
    target_tokens: int = 350
    max_tokens: int = 500
    overlap_tokens: int = 60
    short_clause_tokens: int = 40


@dataclass
class ChunkDraft:
    clause_ordinal: int
    text: str
    token_count: int
    char_start: int  # offsets into the clause text
    char_end: int
    page_start: int | None
    page_end: int | None
    injection_score: float = 0.0
    injection_flags: tuple[str, ...] = ()
    has_table: bool = False
    header_repeated: bool = False  # a table row group that repeats the header row
    ordinal: int = 0  # position within the document, assigned later
    header: str = ""  # the context header embedded in front of `text`


def _overlapping(clause: ClauseDraft, start: int, end: int) -> list[ExtractedBlock]:
    return [block for b_start, b_end, block in clause.spans if b_start < end and b_end > start]


def _draft(
    clause: ClauseDraft, text: str, start: int, end: int, *, header_repeated: bool = False
) -> ChunkDraft:
    blocks = _overlapping(clause, start, end)
    pages = [b.page for b in blocks if b.page is not None]
    flagged = [b for b in blocks if b.injection_flags]
    return ChunkDraft(
        clause_ordinal=clause.ordinal,
        text=text,
        token_count=estimate_tokens(text),
        char_start=start,
        char_end=end,
        page_start=min(pages) if pages else None,
        page_end=max(pages) if pages else None,
        injection_score=max((b.injection_score for b in blocks), default=0.0),
        injection_flags=tuple(sorted({f for b in flagged for f in b.injection_flags})),
        has_table=any(b.kind == "table" for b in blocks),
        header_repeated=header_repeated,
    )


def _pack_sentences(
    clause: ClauseDraft, sentences: list[Span], settings: ChunkSettings
) -> list[ChunkDraft]:
    """Greedy packing: add sentences until the target is reached; start the next chunk with the
    previous chunk's last sentence when that sentence is short enough to overlap."""
    text = clause.text
    chunks: list[ChunkDraft] = []
    i = 0
    while i < len(sentences):
        j = i + 1
        while (
            j < len(sentences)
            and estimate_tokens(text[sentences[i][0] : sentences[j][1]]) <= settings.target_tokens
        ):
            j += 1
        start, end = sentences[i][0], sentences[j - 1][1]
        chunks.append(_draft(clause, text[start:end], start, end))
        last = sentences[j - 1]
        overlap = j - 1 > i and estimate_tokens(text[last[0] : last[1]]) <= settings.overlap_tokens
        i = j - 1 if overlap else j
    return chunks


def _table_chunks(clause: ClauseDraft, span: Span, settings: ChunkSettings) -> list[ChunkDraft]:
    start, end = span
    text = clause.text[start:end]
    if estimate_tokens(text) <= settings.max_tokens:
        return [_draft(clause, text, start, end)]

    # Split by row groups, repeating the header row (and its separator) in every group.
    lines = text.splitlines(keepends=True)
    header, rows = "".join(lines[:2]), lines[2:]
    budget = max(1, settings.max_tokens - estimate_tokens(header))
    groups: list[list[str]] = [[]]
    used = 0
    for row in rows:
        tokens = estimate_tokens(row)
        if groups[-1] and used + tokens > budget:
            groups.append([])
            used = 0
        groups[-1].append(row)
        used += tokens

    chunks: list[ChunkDraft] = []
    offset = start + len(header)
    for index, group in enumerate(groups):
        body = "".join(group)
        piece_start = start if index == 0 else offset
        piece_end = offset + len(body)
        # group 0 is the original text; later groups repeat the header so each stands alone
        chunks.append(
            _draft(
                clause,
                (header + body).rstrip("\n"),
                piece_start,
                piece_end,
                header_repeated=index > 0,
            )
        )
        offset = piece_end
    return chunks


def chunk_clause(clause: ClauseDraft, settings: ChunkSettings | None = None) -> list[ChunkDraft]:
    settings = settings or ChunkSettings()
    text = clause.text
    if not text.strip():
        return []
    if estimate_tokens(text) < settings.short_clause_tokens:
        return [_draft(clause, text, 0, len(text))]  # short clauses stay whole

    chunks: list[ChunkDraft] = []
    run: list[Span] = []

    def flush() -> None:
        if run:
            chunks.extend(_pack_sentences(clause, list(run), settings))
            run.clear()

    for b_start, b_end, block in clause.spans:
        if block.kind == "table":
            flush()
            chunks.extend(_table_chunks(clause, (b_start, b_end), settings))
            continue
        for s_start, s_end in split_sentences(text[b_start:b_end]):
            span = (b_start + s_start, b_start + s_end)
            if estimate_tokens(text[span[0] : span[1]]) > settings.max_tokens:
                run.extend(split_long_span(text, span, settings.max_tokens))
            else:
                run.append(span)
    flush()
    return chunks
