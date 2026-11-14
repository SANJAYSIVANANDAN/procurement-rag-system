import pytest

from covenantiq.core.errors import PermanentTaskError
from covenantiq.ingestion.extraction.quality import compute_quality, require_text
from covenantiq.ingestion.extraction.types import ExtractedBlock


def block(
    text: str, page: int | None, kind: str = "paragraph", score: float = 0.0
) -> ExtractedBlock:
    return ExtractedBlock(kind, text, page, None, (), None, injection_score=score)  # type: ignore[arg-type]


def pages_of_text(pages: int, chars_per_page: int) -> list[ExtractedBlock]:
    return [block("x" * chars_per_page, page) for page in range(1, pages + 1)]


class TestComputeQuality:
    def test_a_healthy_pdf(self) -> None:
        blocks = [*pages_of_text(5, 800), block("1. Heading", 1, "heading")]
        quality = compute_quality(blocks, tables=2, page_count=5, docling_version="2.150.0")
        assert quality.pages_total == 5
        assert quality.pages_with_text == 5
        assert quality.text_chars == 5 * 800 + len("1. Heading")
        assert (quality.tables, quality.headings) == (2, 1)
        assert quality.ocr_needed_pages == ()
        assert not quality.insufficient_text
        assert quality.docling_version == "2.150.0"

    def test_pages_without_text_are_listed_for_ocr(self) -> None:
        blocks = pages_of_text(2, 900) + pages_of_text(1, 900)[:0]
        quality = compute_quality(blocks, tables=0, page_count=4)
        assert quality.pages_with_text == 2
        assert quality.ocr_needed_pages == (3, 4)

    def test_too_few_pages_with_text_is_insufficient(self) -> None:
        # 7 of 10 pages have text (70% < 80%) even though there is plenty of it
        quality = compute_quality(pages_of_text(7, 5000), tables=0, page_count=10)
        assert quality.insufficient_text

    def test_exactly_eighty_percent_of_pages_is_enough(self) -> None:
        quality = compute_quality(pages_of_text(8, 5000), tables=0, page_count=10)
        assert not quality.insufficient_text

    def test_too_little_text_per_page_is_insufficient(self) -> None:
        quality = compute_quality(pages_of_text(10, 150), tables=0, page_count=10)
        assert quality.pages_with_text == 10
        assert quality.insufficient_text  # 150 chars per page < 200

    def test_two_hundred_characters_per_page_is_enough(self) -> None:
        assert not compute_quality(
            pages_of_text(10, 200), tables=0, page_count=10
        ).insufficient_text

    def test_blocks_outside_the_page_range_do_not_count(self) -> None:
        quality = compute_quality([block("x" * 900, 99)], tables=0, page_count=1)
        assert quality.pages_with_text == 0

    def test_whitespace_only_blocks_do_not_make_a_page_a_text_page(self) -> None:
        quality = compute_quality([block("   ", 1)], tables=0, page_count=1)
        assert quality.pages_with_text == 0

    def test_formats_without_pages_report_no_per_page_measures(self) -> None:
        quality = compute_quality([block("Some text.", None)], tables=0, page_count=None)
        assert (quality.pages_total, quality.pages_with_text) == (None, None)
        assert quality.ocr_needed_pages == ()
        assert not quality.insufficient_text
        assert quality.text_chars == len("Some text.")

    def test_flagged_blocks_are_counted(self) -> None:
        blocks = [block("ok", 1), block("bad", 1, score=0.7), block("worse", 1, score=0.5)]
        assert compute_quality(blocks, tables=0, page_count=1).injection_flagged_blocks == 2

    def test_to_dict_has_the_documented_keys_and_is_json_safe(self) -> None:
        import json

        data = compute_quality(
            pages_of_text(2, 900), tables=1, page_count=2, docling_version="x"
        ).to_dict()
        assert set(data) == {
            "pages_total",
            "pages_with_text",
            "text_chars",
            "tables",
            "headings",
            "ocr_needed_pages",
            "insufficient_text",
            "docling_version",
            "injection_flagged_blocks",
        }
        json.dumps(data)


class TestGate:
    def test_no_text_at_all_fails_with_a_user_actionable_code(self) -> None:
        quality = compute_quality([], tables=0, page_count=3)
        with pytest.raises(PermanentTaskError) as exc:
            require_text(quality)
        assert exc.value.job_code == "NO_EXTRACTABLE_TEXT"
        assert "scan" in exc.value.message.lower()

    def test_any_text_passes_even_if_insufficient(self) -> None:
        quality = compute_quality([block("A little text.", 1)], tables=0, page_count=5)
        assert quality.insufficient_text
        require_text(quality)  # insufficient is a warning, not a failure

    def test_a_docx_with_no_text_also_fails(self) -> None:
        with pytest.raises(PermanentTaskError):
            require_text(compute_quality([], tables=0, page_count=None))
