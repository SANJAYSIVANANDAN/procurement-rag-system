from dataclasses import dataclass, field
from typing import Any, Literal

BlockKind = Literal["heading", "paragraph", "list_item", "table"]


@dataclass(frozen=True)
class ExtractedBlock:
    """One unit of document content in reading order, with where it sits in the structure."""

    kind: BlockKind
    text: str
    page: int | None  # None for formats without pages (DOCX)
    level: int | None  # heading depth; None for non-headings
    heading_path: tuple[str, ...]
    clause_ref: str | None
    injection_score: float = 0.0
    injection_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractionQuality:
    """How much real text came out. Stored on the document as `extraction_quality` (JSONB)."""

    pages_total: int | None
    pages_with_text: int | None
    text_chars: int
    tables: int
    headings: int
    ocr_needed_pages: tuple[int, ...]
    insufficient_text: bool
    docling_version: str | None = None
    injection_flagged_blocks: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pages_total": self.pages_total,
            "pages_with_text": self.pages_with_text,
            "text_chars": self.text_chars,
            "tables": self.tables,
            "headings": self.headings,
            "ocr_needed_pages": list(self.ocr_needed_pages),
            "insufficient_text": self.insufficient_text,
            "docling_version": self.docling_version,
            "injection_flagged_blocks": self.injection_flagged_blocks,
        }


@dataclass(frozen=True)
class ExtractionResult:
    blocks: list[ExtractedBlock]
    page_count: int | None
    quality: ExtractionQuality
    warnings: list[str] = field(default_factory=list)
