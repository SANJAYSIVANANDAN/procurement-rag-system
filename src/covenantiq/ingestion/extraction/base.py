from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from covenantiq.ingestion.extraction.types import ExtractionResult

ProgressCallback = Callable[[float], None]


class DocumentExtractor(Protocol):
    """Turns a stored PDF or DOCX into structured blocks.

    Synchronous and CPU-bound: callers run it in a thread. `progress` receives a fraction in
    [0, 1] as long documents are converted in page batches.
    """

    def extract(
        self, path: Path, extension: str, *, progress: ProgressCallback | None = None
    ) -> ExtractionResult: ...
