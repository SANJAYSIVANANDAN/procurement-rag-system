from pathlib import Path

import pytest

from covenantiq.core.errors import (
    EncryptedPdf,
    FileTooLarge,
    TooManyPages,
    UnsupportedFileType,
)
from covenantiq.ingestion.validation import (
    DOCX_MIME,
    PDF_MIME,
    check_content_type,
    file_extension,
    probe_docx,
    probe_pdf,
    probe_structure,
    sanitize_filename,
)
from tests.fixtures import contracts as fixtures
from tests.fixtures import documents


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


class TestExtension:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [("a.pdf", "pdf"), ("A.PDF", "pdf"), ("b.docx", "docx"), ("c.d.DocX", "docx")],
    )
    def test_allowed(self, filename: str, expected: str) -> None:
        assert file_extension(filename) == expected

    @pytest.mark.parametrize(
        "filename", ["a.exe", "a.txt", "a.doc", "a.pdf.exe", "pdf", "", "a.", ".pdf.zip"]
    )
    def test_rejected(self, filename: str) -> None:
        with pytest.raises(UnsupportedFileType, match="PDF and DOCX"):
            file_extension(filename)

    @pytest.mark.parametrize("filename", ["a.docm", "a.DOTM", "a.xlsm"])
    def test_macro_formats_get_their_own_message(self, filename: str) -> None:
        with pytest.raises(UnsupportedFileType, match="Macro-enabled"):
            file_extension(filename)


class TestContentType:
    def test_pdf_and_docx_are_recognised(self) -> None:
        assert check_content_type("pdf", fixtures.make_pdf()) == PDF_MIME
        assert check_content_type("docx", fixtures.make_docx()) == DOCX_MIME

    def test_a_plain_zip_is_accepted_for_docx_and_left_to_the_structure_probe(self) -> None:
        # libmagic reports a DOCX as application/zip when the Word parts are outside its window
        assert check_content_type("docx", fixtures.make_docx_missing_document_part()) == DOCX_MIME

    @pytest.mark.parametrize(
        ("extension", "data"),
        [
            ("pdf", fixtures.WINDOWS_EXECUTABLE),
            ("pdf", fixtures.PLAIN_TEXT),
            ("pdf", fixtures.PNG_IMAGE),
            ("pdf", fixtures.make_docx()),
            ("docx", fixtures.PNG_IMAGE),
            ("docx", fixtures.make_pdf()),
            ("docx", fixtures.WINDOWS_EXECUTABLE),
            ("docx", b""),
        ],
    )
    def test_extension_and_content_must_agree(self, extension: str, data: bytes) -> None:
        with pytest.raises(UnsupportedFileType, match="does not match"):
            check_content_type(extension, data)

    def test_only_the_first_8_kib_are_sniffed(self) -> None:
        assert check_content_type("pdf", fixtures.make_pdf() + b"\x00" * 100_000) == PDF_MIME


class TestPdfProbe:
    def test_counts_pages(self, tmp_path: Path) -> None:
        assert probe_pdf(write(tmp_path, "a.pdf", fixtures.make_pdf(7)), max_pages=300) == 7

    def test_page_limit_is_inclusive(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a.pdf", fixtures.make_pdf(5))
        assert probe_pdf(path, max_pages=5) == 5
        with pytest.raises(TooManyPages) as exc:
            probe_pdf(path, max_pages=4)
        assert exc.value.details == {"pages": 5, "max_pages": 4}

    def test_password_protected_pdf_is_rejected(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a.pdf", fixtures.make_encrypted_pdf("pw", "owner"))
        with pytest.raises(EncryptedPdf):
            probe_pdf(path, max_pages=9)

    def test_owner_password_only_pdf_is_still_encrypted(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a.pdf", fixtures.make_encrypted_pdf("", "owner"))
        with pytest.raises(EncryptedPdf):
            probe_pdf(path, max_pages=9)

    @pytest.mark.parametrize(
        "data", [fixtures.make_corrupt_pdf(), b"%PDF-1.7\n", fixtures.PLAIN_TEXT]
    )
    def test_unreadable_pdf_is_rejected(self, tmp_path: Path, data: bytes) -> None:
        with pytest.raises(UnsupportedFileType, match="corrupt"):
            probe_pdf(write(tmp_path, "a.pdf", data), max_pages=9)


class TestDocxProbe:
    def test_valid_docx_passes(self, tmp_path: Path) -> None:
        probe_docx(write(tmp_path, "a.docx", fixtures.make_docx()))

    def test_macro_project_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(UnsupportedFileType, match="Macro-enabled"):
            probe_docx(write(tmp_path, "a.docx", fixtures.make_docx_with_macros()))

    def test_macro_enabled_content_type_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(UnsupportedFileType, match="Macro-enabled"):
            probe_docx(write(tmp_path, "a.docx", fixtures.make_docm_content_type()))

    def test_zip_without_word_parts_is_not_a_docx(self, tmp_path: Path) -> None:
        with pytest.raises(UnsupportedFileType, match="not a valid DOCX"):
            probe_docx(write(tmp_path, "a.docx", fixtures.make_docx_missing_document_part()))

    def test_truncated_docx_is_corrupt(self, tmp_path: Path) -> None:
        with pytest.raises(UnsupportedFileType, match="corrupt"):
            probe_docx(write(tmp_path, "a.docx", fixtures.make_corrupt_docx()))

    def test_zip_bomb_is_rejected_without_being_extracted(self, tmp_path: Path) -> None:
        bomb = fixtures.make_zip_bomb_docx(150)
        assert len(bomb) < 1024 * 1024  # tiny on disk, 150 MiB when expanded
        with pytest.raises(FileTooLarge):
            probe_docx(write(tmp_path, "a.docx", bomb))


class TestStructureDispatch:
    def test_pdf_reports_pages_and_docx_does_not(self, tmp_path: Path) -> None:
        pdf = write(tmp_path, "a.pdf", fixtures.make_pdf(2))
        docx = write(tmp_path, "a.docx", fixtures.make_docx())
        assert probe_structure(pdf, "pdf", max_pages=10).page_count == 2
        assert probe_structure(docx, "docx", max_pages=10).page_count is None


class TestFilenames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("contract.pdf", "contract.pdf"),
            ("../../etc/passwd", "passwd"),
            ("C:\\Users\\evil\\payload.pdf", "payload.pdf"),
            ("a\x00b\nc.pdf", "abc.pdf"),
            ("  spaced   out .pdf ", "spaced out .pdf"),
            ("", "document"),
            (None, "document"),
            ("\x00\x01", "document"),
        ],
    )
    def test_sanitize(self, raw: str | None, expected: str) -> None:
        assert sanitize_filename(raw) == expected

    def test_long_names_are_truncated(self) -> None:
        assert len(sanitize_filename("x" * 400 + ".pdf")) == 255


class TestRealisticDocx:
    """A DOCX produced by a real word processor library, not hand-assembled: larger than the sniff
    window and with the usual docProps parts. libmagic classifies such files unreliably."""

    def test_a_genuine_docx_passes_the_content_check_and_the_structure_probe(
        self, tmp_path: Path
    ) -> None:
        data = documents.make_msa_docx()
        assert len(data) > 8192  # bigger than what is sniffed
        assert check_content_type("docx", data[:8192]) == DOCX_MIME
        probe_docx(write(tmp_path, "msa.docx", data))

    def test_the_zip_signature_rescues_only_an_unclassified_docx(self) -> None:
        unclassified_zip = b"PK\x03\x04" + bytes(range(256)) * 4
        assert check_content_type("docx", unclassified_zip) == DOCX_MIME
        for impostor in (b"\x00\x01\x02\x03" + bytes(range(256)) * 4, b"MZ" + b"\x90" * 300):
            with pytest.raises(UnsupportedFileType, match="does not match"):
                check_content_type("docx", impostor)

    def test_the_signature_rule_does_not_apply_to_pdf(self) -> None:
        with pytest.raises(UnsupportedFileType, match="does not match"):
            check_content_type("pdf", b"PK\x03\x04" + bytes(range(256)) * 4)

    def test_other_files_are_still_refused_as_docx(self) -> None:
        for data in (fixtures.PNG_IMAGE, fixtures.make_pdf(), fixtures.PLAIN_TEXT):
            with pytest.raises(UnsupportedFileType, match="does not match"):
                check_content_type("docx", data)

    def test_an_unclassified_zip_that_is_not_a_docx_fails_the_structure_probe(
        self, tmp_path: Path
    ) -> None:
        # the sniff alone no longer rejects it, so the probe must
        path = write(tmp_path, "x.docx", fixtures.make_docx_missing_document_part())
        with pytest.raises(UnsupportedFileType, match="not a valid DOCX"):
            probe_docx(path)
