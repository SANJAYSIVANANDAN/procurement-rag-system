"""Reading the dataset's text contracts into the same blocks Docling would produce.

The synthetic contracts are plain text with `@page N` lines, `# ` headings and numbered paragraphs.
The extractor maps them to `RawItem`s and hands them to the production `build_blocks`, so everything
after extraction (clause references, the quality gate, chunking, embedding) is the real code.
"""

import re
from pathlib import Path

from covenantiq.ingestion.extraction.quality import compute_quality
from covenantiq.ingestion.extraction.structure import RawItem, build_blocks
from covenantiq.ingestion.extraction.types import ExtractionResult

_PAGE = re.compile(r"^@page\s+(\d+)\s*$")


class TextExtractor:
    """A `DocumentExtractor` for the evaluation documents."""

    def extract(self, path: Path, extension: str, *, progress: object = None) -> ExtractionResult:
        items: list[RawItem] = []
        page = 1
        titled = False
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            if match := _PAGE.match(line):
                page = int(match.group(1))
            elif line.startswith("# "):
                text = line[2:].strip()
                if not titled and not text[:1].isdigit():
                    titled = True
                    items.append(RawItem("title", text, page))
                else:
                    items.append(RawItem("heading", text, page))
            else:
                items.append(RawItem("text", line, page))
        blocks, tables = build_blocks(items)
        pages = max((i.page or 1 for i in items), default=1)
        return ExtractionResult(
            blocks=blocks,
            page_count=pages,
            quality=compute_quality(blocks, tables=tables, page_count=pages),
        )
