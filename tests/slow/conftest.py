"""Fixtures for tests that run real Docling. Loading its models takes minutes, so one extractor is
warmed up once and shared by every test in the session."""

from pathlib import Path

import pytest

from covenantiq.ingestion.extraction.docling_extractor import DoclingExtractor
from covenantiq.ingestion.extraction.types import ExtractionResult
from tests.fixtures import contracts, documents


@pytest.fixture(scope="session")
def extractor() -> DoclingExtractor:
    shared = DoclingExtractor(page_batch=50)
    shared.warm_up()
    return shared


@pytest.fixture(scope="session")
def files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("slow-fixtures")
    contents = {
        "msa.pdf": documents.make_msa_pdf(),
        "msa.docx": documents.make_msa_docx(),
        "scanned.pdf": documents.make_scanned_pdf(),
        "injection.pdf": documents.make_injection_pdf(),
        "corrupt.pdf": contracts.make_corrupt_pdf(),
        "corrupt.docx": contracts.make_corrupt_docx(),
    }
    paths = {}
    for name, data in contents.items():
        paths[name] = root / name
        paths[name].write_bytes(data)
    return paths


@pytest.fixture(scope="session")
def msa_pdf(extractor: DoclingExtractor, files: dict[str, Path]) -> ExtractionResult:
    return extractor.extract(files["msa.pdf"], "pdf")


@pytest.fixture(scope="session")
def msa_docx(extractor: DoclingExtractor, files: dict[str, Path]) -> ExtractionResult:
    return extractor.extract(files["msa.docx"], "docx")
