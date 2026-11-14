"""Turning retrieved chunks into the numbered sources the model sees (spec 19.9).

    <source id="C1" document="Acme Cloud MSA 2026" clause="12.2" pages="14">
    ...exact clause text...
    </source>

The model is told that anything inside `<source>` is untrusted document content. To keep a document
from closing the tag itself, a literal `<source` or `</source` inside the text is escaped.
"""

import re
from collections.abc import Sequence
from uuid import UUID

from covenantiq.ingestion.chunking.tokens import estimate_tokens
from covenantiq.ingestion.injection_scan import FLAG_THRESHOLD
from covenantiq.rag.types import (
    ContextBundle,
    ContextSource,
    ParentClause,
    RetrievalResult,
    RetrievedChunk,
)

TAG_OVERHEAD_TOKENS = 25  # the <source ...> wrapper around each group
CHUNK_SEPARATOR = "\n\n"
INJECTION_WARNING = "possible embedded instructions"

_TAG_LIKE = re.compile(r"<(/?)source", re.IGNORECASE)


def escape_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", " ")
    )


def escape_text(text: str) -> str:
    """Stop document text from opening or closing a source block."""
    return _TAG_LIKE.sub(lambda m: f"&lt;{m.group(1)}source", text)


def _pages(
    chunks: Sequence[RetrievedChunk], parent: ParentClause | None
) -> tuple[int | None, int | None]:
    if parent is not None and parent.page_start is not None:
        return parent.page_start, parent.page_end
    starts = [c.page_start for c in chunks if c.page_start is not None]
    ends = [c.page_end for c in chunks if c.page_end is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _page_label(start: int | None, end: int | None) -> str:
    if start is None:
        return ""
    return str(start) if end is None or end == start else f"{start}-{end}"


def _render(source: ContextSource) -> str:
    attributes = [
        f'id="{source.marker}"',
        f'document="{escape_attribute(source.document_title)}"',
    ]
    if source.clause_ref:
        attributes.append(f'clause="{escape_attribute(source.clause_ref)}"')
    pages = _page_label(source.page_start, source.page_end)
    if pages:
        attributes.append(f'pages="{pages}"')
    if source.injection_flagged:
        attributes.append(f'warning="{INJECTION_WARNING}"')
    return f"<source {' '.join(attributes)}>\n{escape_text(source.text)}\n</source>"


def build_context(
    result: RetrievalResult,
    *,
    max_tokens: int,
    parent_max_tokens: int,
    expand_parents: bool = True,
) -> ContextBundle:
    """Group the final chunks by clause, in rank order, and number them `C1..Cn` until the token
    budget is spent.

    A group shows its whole parent clause when that is short enough; otherwise only the matched
    chunks plus their neighbours. A group that does not fit is dropped whole, never cut in the
    middle of a sentence, and so is everything ranked below it.
    """
    groups: dict[UUID, list[RetrievedChunk]] = {}
    for chunk in result.chunks:
        groups.setdefault(chunk.clause_id, []).append(chunk)

    sources: list[ContextSource] = []
    rendered: list[str] = []
    used = 0
    dropped = 0
    for index, (clause_id, matched) in enumerate(groups.items()):
        parent = result.parents.get(clause_id)
        if expand_parents and parent is not None and parent.token_count <= parent_max_tokens:
            text, pieces = parent.text, matched
        else:
            seen: dict[UUID, RetrievedChunk] = {}
            for chunk in matched:
                for piece in [*result.neighbours.get(chunk.chunk_id, []), chunk]:
                    seen.setdefault(piece.chunk_id, piece)
            pieces = sorted(seen.values(), key=lambda c: c.ordinal)
            text = CHUNK_SEPARATOR.join(piece.text for piece in pieces)
            parent = None
        cost = estimate_tokens(text) + TAG_OVERHEAD_TOKENS
        if used + cost > max_tokens:
            dropped = len(groups) - index
            break
        first = matched[0]
        start, end = _pages(pieces, parent)
        source = ContextSource(
            marker=f"C{len(sources) + 1}",
            chunk_ids=tuple(c.chunk_id for c in matched),
            clause_id=clause_id,
            document_id=first.document_id,
            document_title=first.document_title,
            clause_ref=first.clause_ref,
            page_start=start,
            page_end=end,
            text=text,
            injection_flagged=any(c.injection_score >= FLAG_THRESHOLD for c in pieces),
        )
        sources.append(source)
        rendered.append(_render(source))
        used += cost
    return ContextBundle(
        text="\n\n".join(rendered), sources=sources, tokens=used, dropped_groups=dropped
    )
