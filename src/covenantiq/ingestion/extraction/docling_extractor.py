"""Docling-backed extraction (spec 18.5).

Docling is imported lazily, inside methods: importing it (and PyTorch) takes seconds to minutes,
which the API process must never pay for, and unit tests of everything else should not either.
"""

import threading
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium

from covenantiq.core.errors import PermanentTaskError
from covenantiq.core.logging import get_logger
from covenantiq.ingestion.extraction.base import ProgressCallback
from covenantiq.ingestion.extraction.quality import compute_quality
from covenantiq.ingestion.extraction.structure import RawItem, RawLabel, build_blocks
from covenantiq.ingestion.extraction.types import ExtractionResult

log = get_logger(__name__)

# Docling's item labels (DocItemLabel values) mapped to ours. Anything else, such as pictures and
# formulas, carries no contract text and is skipped.
_LABELS: dict[str, RawLabel] = {
    "title": "title",
    "section_header": "heading",
    "list_item": "list_item",
    "text": "text",
    "paragraph": "text",
    "caption": "text",
    "footnote": "text",
    "reference": "text",
    "code": "text",
    "table": "table",
    "page_header": "page_header",
    "page_footer": "page_footer",
}


def docling_version() -> str | None:
    try:
        return version("docling")
    except PackageNotFoundError:
        return None


def page_ranges(total_pages: int, batch: int) -> list[tuple[int, int]]:
    """Inclusive 1-based page ranges of at most `batch` pages covering the document."""
    return [
        (start, min(start + batch - 1, total_pages)) for start in range(1, total_pages + 1, batch)
    ]


class DoclingExtractor:
    """Converts PDFs and DOCX files with one Docling converter per process.

    OCR is off (scanned PDFs are reported, not read: spec 18.5). Large PDFs are converted in page
    batches to bound memory, and the batches are merged in order so headings carry across them.
    """

    def __init__(
        self,
        *,
        page_batch: int = 50,
        artifacts_path: str | None = None,
        accurate_tables: bool = True,
    ) -> None:
        self._page_batch = page_batch
        self._artifacts_path = artifacts_path
        self._accurate_tables = accurate_tables
        self._converter: Any = None
        self._lock = threading.Lock()  # a converter is not safe for concurrent conversions

    def warm_up(self) -> None:
        """Load the models now, so the first job does not pay for it (called once per worker
        process at start-up)."""
        from docling.datamodel.base_models import InputFormat

        with self._lock:
            self._get_converter().initialize_pipeline(InputFormat.PDF)

    def extract(
        self, path: Path, extension: str, *, progress: ProgressCallback | None = None
    ) -> ExtractionResult:
        with self._lock:
            try:
                return self._extract(path, extension, progress)
            except PermanentTaskError:
                raise
            except Exception as exc:
                log.error("extraction.failed", error=type(exc).__name__)
                raise PermanentTaskError(
                    "The document could not be read.", job_code="EXTRACTION_FAILED"
                ) from exc

    def _extract(
        self, path: Path, extension: str, progress: ProgressCallback | None
    ) -> ExtractionResult:
        converter = self._get_converter()
        items: list[RawItem] = []
        page_count: int | None = None

        if extension == "pdf":
            page_count = self._pdf_page_count(path)
            ranges = page_ranges(page_count, self._page_batch)
            for number, (first, last) in enumerate(ranges, start=1):
                result = converter.convert(str(path), page_range=(first, last))
                items.extend(self._items(result.document))
                if progress is not None:
                    progress(number / len(ranges))
        else:
            result = converter.convert(str(path))
            items.extend(self._items(result.document))
            if progress is not None:
                progress(1.0)

        blocks, tables = build_blocks(items)
        quality = compute_quality(
            blocks, tables=tables, page_count=page_count, docling_version=docling_version()
        )
        return ExtractionResult(blocks=blocks, page_count=page_count, quality=quality)

    @staticmethod
    def _pdf_page_count(path: Path) -> int:
        document = pdfium.PdfDocument(str(path))
        try:
            return len(document)
        finally:
            document.close()

    def _get_converter(self) -> Any:
        if self._converter is None:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
            from docling.document_converter import (
                DocumentConverter,
                PdfFormatOption,
                WordFormatOption,
            )

            options = PdfPipelineOptions(do_ocr=False, do_table_structure=True)
            options.table_structure_options.mode = (  # type: ignore[attr-defined]
                TableFormerMode.ACCURATE if self._accurate_tables else TableFormerMode.FAST
            )
            if self._artifacts_path:
                options.artifacts_path = self._artifacts_path
            self._converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=options),
                    InputFormat.DOCX: WordFormatOption(),
                }
            )
        return self._converter

    @staticmethod
    def _items(document: Any) -> list[RawItem]:
        """Flatten a DoclingDocument into reading-order RawItems."""
        items: list[RawItem] = []
        for item, _depth in document.iterate_items():
            label = _LABELS.get(str(getattr(item.label, "value", item.label)))
            if label is None:
                continue
            provenance = getattr(item, "prov", None)
            page = provenance[0].page_no if provenance else None

            if label == "table":
                items.append(
                    RawItem("table", "", page, None, item.export_to_markdown(doc=document))
                )
                continue

            text = getattr(item, "text", "") or ""
            if label == "list_item" and getattr(item, "enumerated", False):
                # Docling strips the numbering ("1.1") into `marker`; the clause reference lives
                # there, so put it back at the front of the text.
                marker = getattr(item, "marker", "") or ""
                text = f"{marker} {text}".strip()
            items.append(RawItem(label, text, page, getattr(item, "level", None)))
        return items
