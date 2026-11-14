"""Turns an extractor's flat stream of items into structured blocks (spec 18.5 "Traversal").

Pure logic with no dependency on Docling: the Docling adapter maps its items to `RawItem`, so the
heading stack, clause-reference parsing, header/footer removal and table splitting can be tested
without models.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from covenantiq.ingestion.extraction.types import BlockKind, ExtractedBlock
from covenantiq.ingestion.injection_scan import ScanResult, scan

RawLabel = Literal["title", "heading", "text", "list_item", "table", "page_header", "page_footer"]

MAX_TABLE_ROWS = 60

_NUMBERED_HEADING = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3})*)[.)]?(?=\s|$)")
_NUMBERED_PARAGRAPH = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3})+)[.)]?(?=\s)")
_SCHEDULE = re.compile(
    r"^\s*(Schedule|Annex|Appendix|Exhibit|Attachment)\s+([A-Z]|\d{1,3})\b", re.I
)
_ARTICLE = re.compile(
    r"^\s*(Article|Section|Clause)\s+(\d{1,3}(?:\.\d{1,3})*|[IVXLC]{1,6})\b", re.I
)
_SUBCLAUSE = re.compile(r"^\s*\(([a-z]{1,2}|[ivxlc]{1,6}|\d{1,2})\)(?=\s)")


@dataclass(frozen=True)
class RawItem:
    """What an extractor reports, in reading order."""

    label: RawLabel
    text: str = ""
    page: int | None = None
    level: int | None = None  # the extractor's own heading level, if it gives one
    markdown: str | None = None  # tables only


def parse_heading_ref(text: str) -> str | None:
    """The clause reference a heading carries: `12.3`, `Schedule 2`, `Article IV`, or None."""
    if match := _NUMBERED_HEADING.match(text):
        return match.group(1)
    if match := _SCHEDULE.match(text):
        return f"{match.group(1).capitalize()} {match.group(2).upper()}"
    if match := _ARTICLE.match(text):
        return f"{match.group(1).capitalize()} {match.group(2).upper()}"
    return None


def parse_paragraph_ref(text: str, parent_ref: str | None) -> str | None:
    """The reference a paragraph starts with: a dotted number (`12.3`) or a sub-clause marker
    (`(a)`, made relative to its parent as `12.3(a)`). A bare number is not a reference."""
    if match := _NUMBERED_PARAGRAPH.match(text):
        return match.group(1)
    if match := _SUBCLAUSE.match(text):
        marker = f"({match.group(1)})"
        return f"{parent_ref}{marker}" if parent_ref else marker
    return None


def heading_depth(ref: str | None) -> int | None:
    """How deep a numbered reference sits: `12` is 1, `12.3` is 2, `12.3.1` is 3."""
    if ref is None or not ref[0].isdigit():
        return 1 if ref else None
    return ref.count(".") + 1


def _clean(text: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def split_markdown_table(markdown: str, max_rows: int = MAX_TABLE_ROWS) -> list[str]:
    """Split a markdown table into groups of at most `max_rows` data rows, repeating the header."""
    lines = [line for line in markdown.strip().splitlines() if line.strip()]
    if len(lines) < 3 or not set(lines[1].replace("|", "").strip()) <= set("-: "):
        return [markdown.strip()] if markdown.strip() else []
    header, rows = lines[:2], lines[2:]
    if len(rows) <= max_rows:
        return ["\n".join(lines)]
    return ["\n".join(header + rows[i : i + max_rows]) for i in range(0, len(rows), max_rows)]


@dataclass
class _Heading:
    level: int
    text: str
    ref: str | None


def build_blocks(items: Iterable[RawItem]) -> tuple[list[ExtractedBlock], int]:
    """Walk items in reading order, tracking the heading stack. Returns (blocks, table count)."""
    blocks: list[ExtractedBlock] = []
    stack: list[_Heading] = []
    tables = 0

    def path() -> tuple[str, ...]:
        return tuple(heading.text for heading in stack)

    def current_ref() -> str | None:
        for heading in reversed(stack):
            if heading.ref:
                return heading.ref
        return None

    def emit(
        kind: BlockKind,
        text: str,
        page: int | None,
        level: int | None,
        ref: str | None,
        result: ScanResult,
    ) -> None:
        blocks.append(
            ExtractedBlock(
                kind=kind,
                text=text,
                page=page,
                level=level,
                heading_path=path(),
                clause_ref=ref,
                injection_score=result.score,
                injection_flags=result.flags,
            )
        )

    for item in items:
        if item.label in ("page_header", "page_footer"):
            continue

        if item.label == "table":
            if not item.markdown or not item.markdown.strip():
                continue
            tables += 1
            for group in split_markdown_table(item.markdown):
                found = scan(group)  # scanned as extracted, so hidden characters are reported
                emit("table", found.text, item.page, None, current_ref(), found)
            continue

        found = scan(item.text)
        text = _clean(found.text)
        if not text:
            continue

        if item.label == "title":
            emit("heading", text, item.page, 0, None, found)  # the document's name, not a path part
        elif item.label == "heading":
            ref = parse_heading_ref(text)
            level = heading_depth(ref) or item.level or 1
            while stack and stack[-1].level >= level:
                stack.pop()
            stack.append(_Heading(level, text, ref))
            emit("heading", text, item.page, level, ref, found)
        else:
            kind: BlockKind = "list_item" if item.label == "list_item" else "paragraph"
            parent_ref = current_ref()
            ref = parse_paragraph_ref(text, parent_ref) or parent_ref
            emit(kind, text, item.page, None, ref, found)

    return blocks, tables
