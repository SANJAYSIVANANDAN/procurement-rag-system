"""Clause building: the parent units of retrieval (spec 18.6 steps 1 and 2)."""

from dataclasses import dataclass, field
from statistics import median

from covenantiq.ingestion.chunking.tokens import estimate_tokens
from covenantiq.ingestion.extraction.types import ExtractedBlock
from covenantiq.ingestion.taxonomy import Taxonomy

BLOCK_SEPARATOR = "\n\n"
MIN_MEDIAN_CLAUSE_TOKENS = 80
CANDIDATE_DEPTHS = (3, 2, 1)


@dataclass
class ClauseDraft:
    """A clause before it is stored: a heading and the blocks that belong under it."""

    ordinal: int
    clause_ref: str | None
    heading: str | None
    heading_path: tuple[str, ...]
    blocks: list[ExtractedBlock]
    clause_types: list[str] = field(default_factory=list)
    text: str = ""
    # (start, end, block) character spans of each block inside `text`
    spans: list[tuple[int, int, ExtractedBlock]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.text:
            self._layout()

    def _layout(self) -> None:
        parts: list[str] = []
        self.spans = []
        position = 0
        for block in self.blocks:
            if parts:
                position += len(BLOCK_SEPARATOR)
            self.spans.append((position, position + len(block.text), block))
            parts.append(block.text)
            position += len(block.text)
        self.text = BLOCK_SEPARATOR.join(parts)

    @property
    def pages(self) -> list[int]:
        return [b.page for b in self.blocks if b.page is not None]

    @property
    def page_start(self) -> int | None:
        return min(self.pages) if self.pages else None

    @property
    def page_end(self) -> int | None:
        return max(self.pages) if self.pages else None

    @property
    def has_table(self) -> bool:
        return any(b.kind == "table" for b in self.blocks)


def group_blocks(blocks: list[ExtractedBlock], depth: int) -> list[list[ExtractedBlock]]:
    """Group consecutive blocks: a heading at level 1..`depth` starts a new clause. Deeper
    headings, and the document title (level 0), stay inside the clause they appear in."""
    groups: list[list[ExtractedBlock]] = []
    for block in blocks:
        starts_clause = (
            block.kind == "heading" and block.level is not None and 1 <= block.level <= depth
        )
        if starts_clause or not groups:
            groups.append([block])
        else:
            groups[-1].append(block)
    return groups


def choose_heading_depth(blocks: list[ExtractedBlock], configured: int | None = None) -> int:
    """The deepest heading level (1 to 3) whose clauses have a median length of at least 80
    tokens, or the configured level. Falls back to 1: whole top-level sections."""
    if configured is not None:
        return configured
    for depth in CANDIDATE_DEPTHS:
        groups = [g for g in group_blocks(blocks, depth) if g[0].kind == "heading"]
        if not groups:
            continue
        lengths = [estimate_tokens(BLOCK_SEPARATOR.join(b.text for b in g)) for g in groups]
        if median(lengths) >= MIN_MEDIAN_CLAUSE_TOKENS:
            return depth
    return 1


def _split_oversized(group: list[ExtractedBlock], max_tokens: int) -> list[list[ExtractedBlock]]:
    """Split a clause longer than `max_tokens` at block boundaries (sub-clauses and paragraphs
    are separate blocks). A single block longer than the limit is left whole: the chunker splits
    it by sentence."""
    pieces: list[list[ExtractedBlock]] = [[]]
    used = 0
    for block in group:
        tokens = estimate_tokens(block.text)
        if pieces[-1] and used + tokens > max_tokens:
            pieces.append([])
            used = 0
        pieces[-1].append(block)
        used += tokens
    return pieces


def build_clauses(
    blocks: list[ExtractedBlock],
    *,
    heading_depth: int,
    parent_max_tokens: int,
    taxonomy: Taxonomy,
) -> list[ClauseDraft]:
    clauses: list[ClauseDraft] = []
    for group in group_blocks(blocks, heading_depth):
        head = group[0] if group[0].kind == "heading" and (group[0].level or 0) >= 1 else None
        base_ref = head.clause_ref if head else None
        heading = head.text if head else None
        path = head.heading_path if head else ()

        pieces = (
            _split_oversized(group, parent_max_tokens)
            if estimate_tokens(BLOCK_SEPARATOR.join(b.text for b in group)) > parent_max_tokens
            else [group]
        )
        for index, piece in enumerate(pieces, start=1):
            if not any(b.text.strip() for b in piece):
                continue
            ref = base_ref if index == 1 or base_ref is None else f"{base_ref}#{index}"
            clause = ClauseDraft(
                ordinal=len(clauses),
                clause_ref=ref,
                heading=heading,
                heading_path=path,
                blocks=piece,
            )
            body = (
                clause.text[len(heading) :]
                if heading and clause.text.startswith(heading)
                else clause.text
            )
            clause.clause_types = taxonomy.classify(heading, body)
            clauses.append(clause)
    return clauses
