from covenantiq.core.errors import PermanentTaskError
from covenantiq.ingestion.extraction.types import ExtractedBlock, ExtractionQuality
from covenantiq.ingestion.injection_scan import FLAG_THRESHOLD

MIN_PAGES_WITH_TEXT_RATIO = 0.8
MIN_CHARS_PER_PAGE = 200


def compute_quality(
    blocks: list[ExtractedBlock],
    *,
    tables: int,
    page_count: int | None,
    docling_version: str | None = None,
) -> ExtractionQuality:
    """Measure how much real text was extracted (spec 18.5 "Quality gate").

    `page_count` is None for formats without pages (DOCX); per-page measures are then None and
    only a completely empty extraction counts as a problem.
    """
    text_chars = sum(len(block.text) for block in blocks)
    headings = sum(1 for block in blocks if block.kind == "heading")
    flagged = sum(1 for block in blocks if block.injection_score >= FLAG_THRESHOLD)

    if page_count is None or page_count < 1:
        return ExtractionQuality(
            pages_total=None,
            pages_with_text=None,
            text_chars=text_chars,
            tables=tables,
            headings=headings,
            ocr_needed_pages=(),
            insufficient_text=False,
            docling_version=docling_version,
            injection_flagged_blocks=flagged,
        )

    pages_with_text = {
        block.page
        for block in blocks
        if block.page is not None and block.text.strip() and 1 <= block.page <= page_count
    }
    missing = tuple(page for page in range(1, page_count + 1) if page not in pages_with_text)
    insufficient = (
        len(pages_with_text) / page_count < MIN_PAGES_WITH_TEXT_RATIO
        or text_chars / page_count < MIN_CHARS_PER_PAGE
    )
    return ExtractionQuality(
        pages_total=page_count,
        pages_with_text=len(pages_with_text),
        text_chars=text_chars,
        tables=tables,
        headings=headings,
        ocr_needed_pages=missing,
        insufficient_text=insufficient,
        docling_version=docling_version,
        injection_flagged_blocks=flagged,
    )


def require_text(quality: ExtractionQuality) -> None:
    """A document with no extractable text at all cannot be analysed. OCR is out of scope for V1,
    so a scanned PDF fails here, with a code the user can act on."""
    if quality.text_chars == 0:
        raise PermanentTaskError(
            "No text could be extracted. The document may be a scan; OCR is not supported yet.",
            job_code="NO_EXTRACTABLE_TEXT",
        )
