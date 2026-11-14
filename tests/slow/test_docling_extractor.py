"""Extraction against real documents, with real Docling models. Run with `pytest -m slow`."""

import re
from pathlib import Path

import pytest

from covenantiq.core.errors import PermanentTaskError
from covenantiq.ingestion.extraction.docling_extractor import DoclingExtractor
from covenantiq.ingestion.extraction.quality import require_text
from covenantiq.ingestion.extraction.types import ExtractedBlock, ExtractionResult
from tests.fixtures import documents

pytestmark = pytest.mark.slow


def headings(result: ExtractionResult) -> dict[str, ExtractedBlock]:
    return {b.clause_ref: b for b in result.blocks if b.kind == "heading" and b.clause_ref}


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class TestMsaPdf:
    def test_every_numbered_section_has_the_right_heading_path_clause_ref_and_page(
        self, msa_pdf: ExtractionResult
    ) -> None:
        found = headings(msa_pdf)
        for section in documents.SECTIONS:
            title = f"{section.number}. {section.title}"
            block = found.get(str(section.number))
            assert block is not None, f"section {section.number} was not found as a heading"
            assert block.text == title
            assert block.heading_path == (title,)
            assert block.page == section.page, (title, block.page)

    def test_numbered_sub_clauses_keep_their_numbers(self, msa_pdf: ExtractionResult) -> None:
        # Docling strips "1.1" out of the text into a list marker; extraction must put it back
        by_ref = {b.clause_ref: b for b in msa_pdf.blocks if b.kind != "heading"}
        for section in documents.SECTIONS:
            title = f"{section.number}. {section.title}"
            for clause in section.clauses:
                ref = clause.split(" ", 1)[0]
                block = by_ref.get(ref)
                assert block is not None, f"clause {ref} missing"
                assert squash(block.text) == squash(clause)
                assert block.heading_path == (title,)

    def test_paragraph_page_numbers_are_correct(self, msa_pdf: ExtractionResult) -> None:
        sentence = next(
            b for b in msa_pdf.blocks if documents.LIABILITY_CAP_SENTENCE in squash(b.text)
        )
        assert sentence.page == documents.LIABILITY_CAP_PAGE
        assert sentence.clause_ref == "6.2"
        for section in documents.SECTIONS:
            for clause in section.clauses:
                block = next(b for b in msa_pdf.blocks if b.clause_ref == clause.split(" ", 1)[0])
                assert block.page == section.page or block.page == section.page + 1

    def test_the_fee_table_becomes_a_markdown_table_under_its_section(
        self, msa_pdf: ExtractionResult
    ) -> None:
        tables = [b for b in msa_pdf.blocks if b.kind == "table"]
        assert len(tables) == 1
        table = tables[0]
        assert table.page == 3
        assert table.heading_path == ("5. Fee Schedule",)
        assert table.clause_ref == "5"
        lines = table.text.splitlines()
        assert lines[0].replace(" ", "").startswith("|Service|Unit|Annualfee|")
        assert set(lines[1].replace("|", "").strip()) <= set("-: ")
        for _, unit, fee in documents.FEE_TABLE[1:]:
            assert any(unit in line and fee in line for line in lines)
        assert msa_pdf.quality.tables == 1

    def test_page_headers_and_footers_are_removed(self, msa_pdf: ExtractionResult) -> None:
        for block in msa_pdf.blocks:
            assert documents.HEADER_TEXT not in block.text
            assert not re.fullmatch(r"Page \d+", block.text.strip())

    def test_quality_metrics(self, msa_pdf: ExtractionResult) -> None:
        quality = msa_pdf.quality
        assert msa_pdf.page_count == 5
        assert (quality.pages_total, quality.pages_with_text) == (5, 5)
        assert quality.ocr_needed_pages == ()
        assert not quality.insufficient_text
        assert quality.headings >= 10
        assert quality.docling_version
        assert quality.injection_flagged_blocks == 0

    def test_ordinary_contract_text_is_not_flagged(self, msa_pdf: ExtractionResult) -> None:
        assert all(b.injection_score < 0.5 for b in msa_pdf.blocks)


class TestMsaDocx:
    def test_headings_and_clause_refs(self, msa_docx: ExtractionResult) -> None:
        found = headings(msa_docx)
        for section in documents.SECTIONS:
            title = f"{section.number}. {section.title}"
            block = found.get(str(section.number))
            assert block is not None, title
            assert (block.text, block.heading_path) == (title, (title,))

    def test_docx_has_no_pages(self, msa_docx: ExtractionResult) -> None:
        assert msa_docx.page_count is None
        assert all(b.page is None for b in msa_docx.blocks)
        assert msa_docx.quality.pages_total is None
        assert not msa_docx.quality.insufficient_text

    def test_sub_clauses_and_the_table(self, msa_docx: ExtractionResult) -> None:
        by_ref = {b.clause_ref: b for b in msa_docx.blocks if b.kind == "paragraph"}
        assert "6.2" in by_ref
        assert documents.LIABILITY_CAP_SENTENCE in squash(by_ref["6.2"].text)
        (table,) = [b for b in msa_docx.blocks if b.kind == "table"]
        assert table.heading_path == ("5. Fee Schedule",)
        assert "Platform subscription" in table.text

    def test_headers_and_footers_are_not_extracted(self, msa_docx: ExtractionResult) -> None:
        joined = "\n".join(b.text for b in msa_docx.blocks)
        assert documents.HEADER_TEXT not in joined
        assert "do not extract" not in joined


class TestScannedPdf:
    def test_a_scan_yields_no_text_and_fails_the_gate(
        self, extractor: DoclingExtractor, files: dict[str, Path]
    ) -> None:
        result = extractor.extract(files["scanned.pdf"], "pdf")
        assert result.blocks == []
        assert result.quality.text_chars == 0
        assert result.page_count == 2
        assert result.quality.ocr_needed_pages == (1, 2)
        with pytest.raises(PermanentTaskError) as exc:
            require_text(result.quality)
        assert exc.value.job_code == "NO_EXTRACTABLE_TEXT"


class TestInjection:
    def test_the_injection_fixture_has_a_flagged_block(
        self, extractor: DoclingExtractor, files: dict[str, Path]
    ) -> None:
        result = extractor.extract(files["injection.pdf"], "pdf")
        flagged = [b for b in result.blocks if b.injection_score >= 0.5]
        assert len(flagged) == 1
        (block,) = flagged
        assert block.clause_ref == "3.1"
        assert block.heading_path == ("3. Miscellaneous",)
        assert {"ignore_instructions", "you_are_now"} <= set(block.injection_flags)
        assert result.quality.injection_flagged_blocks == 1
        # the surrounding, ordinary clauses are not flagged
        assert all(b.injection_score < 0.5 for b in result.blocks if b is not block)


class TestBatching:
    def test_page_batches_merge_into_the_same_result(
        self, extractor: DoclingExtractor, files: dict[str, Path], msa_pdf: ExtractionResult
    ) -> None:
        batched = DoclingExtractor(page_batch=2)
        batched._converter = extractor._converter  # share the loaded models
        fractions: list[float] = []

        result = batched.extract(files["msa.pdf"], "pdf", progress=fractions.append)

        assert fractions == pytest.approx([1 / 3, 2 / 3, 1.0])  # 5 pages in batches of 2
        assert result.page_count == 5

        def signature(blocks: list[ExtractedBlock]) -> list[tuple[object, ...]]:
            return [(b.kind, squash(b.text), b.page, b.clause_ref, b.heading_path) for b in blocks]

        assert signature(result.blocks) == signature(msa_pdf.blocks)

    def test_a_single_batch_reports_progress_once(
        self, extractor: DoclingExtractor, files: dict[str, Path]
    ) -> None:
        fractions: list[float] = []
        extractor.extract(files["msa.pdf"], "pdf", progress=fractions.append)
        assert fractions == [1.0]


class TestFailures:
    @pytest.mark.parametrize(
        ("name", "extension"), [("corrupt.pdf", "pdf"), ("corrupt.docx", "docx")]
    )
    def test_a_corrupt_file_fails_with_extraction_failed(
        self, extractor: DoclingExtractor, files: dict[str, Path], name: str, extension: str
    ) -> None:
        with pytest.raises(PermanentTaskError) as exc:
            extractor.extract(files[name], extension)
        assert exc.value.job_code == "EXTRACTION_FAILED"
        assert "docling" not in exc.value.message.lower()  # no internals in the message
