"""Upload validation (spec 18.2). Cheap checks only: nothing here extracts text.

Order: extension, then content sniffing of the first 8 KiB, then (once the file is on disk) a
structure probe. Each failure raises a domain error with the code the API reports.
"""

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

import magic
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from covenantiq.core.errors import (
    EncryptedPdf,
    FileTooLarge,
    TooManyPages,
    UnsupportedFileType,
)

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

SNIFF_BYTES = 8192
MAX_FILENAME_LENGTH = 255
MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_DOCX_ENTRIES = 5000
MAX_DOCX_COMPRESSION_RATIO = 1000
MAX_CONTENT_TYPES_BYTES = 1024 * 1024
PDFIUM_PASSWORD_ERROR = 4

# libmagic is unreliable for DOCX: it reports a plain zip when the Word entries are outside its
# window, and some builds report `application/octet-stream` even for a complete, genuine Word
# file. The structure probe below is what really confirms a DOCX, so for the zip-based format
# the sniff only has to establish "this is a zip", by libmagic's verdict or by the ZIP signature.
ACCEPTED_MIME = {
    "pdf": frozenset({PDF_MIME}),
    "docx": frozenset({DOCX_MIME, "application/zip"}),
}
ZIP_SIGNATURE = b"PK\x03\x04"
UNCLASSIFIED_MIME = "application/octet-stream"
CANONICAL_MIME = {"pdf": PDF_MIME, "docx": DOCX_MIME}

MACRO_EXTENSIONS = frozenset({"docm", "dotm", "xlsm", "pptm"})
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class StructureInfo:
    page_count: int | None


def sanitize_filename(raw: str | None) -> str:
    """The name is kept only as display metadata; it never takes part in a storage path."""
    if not raw:
        return "document"
    name = PureWindowsPath(PurePosixPath(raw).name).name
    name = _CONTROL_CHARACTERS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:MAX_FILENAME_LENGTH] or "document"


def file_extension(filename: str) -> str:
    """The lower-case extension without the dot, if it is on the allow-list."""
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension in MACRO_EXTENSIONS:
        raise UnsupportedFileType("Macro-enabled documents are not accepted.")
    if extension not in ACCEPTED_MIME:
        raise UnsupportedFileType(
            "Only PDF and DOCX files are accepted.", details={"allowed": [".pdf", ".docx"]}
        )
    return extension


def check_content_type(extension: str, head: bytes) -> str:
    """Sniff the first bytes with libmagic and require agreement with the extension.

    Returns the canonical MIME type to store.
    """
    detected = magic.from_buffer(head[:SNIFF_BYTES], mime=True)
    is_zip_by_signature = (
        extension == "docx" and detected == UNCLASSIFIED_MIME and head.startswith(ZIP_SIGNATURE)
    )
    if detected not in ACCEPTED_MIME[extension] and not is_zip_by_signature:
        raise UnsupportedFileType(
            f"The file content does not match a .{extension} file.",
            details={"detected": detected},
        )
    return CANONICAL_MIME[extension]


def probe_pdf(path: Path, *, max_pages: int) -> int:
    """Page count of a readable, unencrypted PDF within the page limit."""
    try:
        document = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == PDFIUM_PASSWORD_ERROR:
            raise EncryptedPdf("Password-protected PDFs are not accepted.") from exc
        raise UnsupportedFileType("The PDF could not be read; it may be corrupt.") from exc
    try:
        # Owner-password-only PDFs open without a password but are still encrypted.
        if pdfium_raw.FPDF_GetSecurityHandlerRevision(document.raw) != -1:
            raise EncryptedPdf("Encrypted PDFs are not accepted.")
        pages = len(document)
    finally:
        document.close()
    if pages < 1:
        raise UnsupportedFileType("The PDF has no pages.")
    if pages > max_pages:
        raise TooManyPages(
            f"The PDF has {pages} pages; the limit is {max_pages}.",
            details={"pages": pages, "max_pages": max_pages},
        )
    return pages


def probe_docx(path: Path) -> None:
    """Confirm a DOCX is a sound, macro-free zip that cannot expand into a zip bomb."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ENTRIES:
                raise UnsupportedFileType("The DOCX contains too many parts.")
            for entry in entries:
                if entry.file_size > MAX_DOCX_COMPRESSION_RATIO * max(entry.compress_size, 1):
                    raise FileTooLarge("The DOCX has a suspiciously high compression ratio.")
            if sum(entry.file_size for entry in entries) > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise FileTooLarge("The DOCX expands to more than 100 MB.")

            names = {entry.filename for entry in entries}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise UnsupportedFileType("The file is not a valid DOCX document.")
            if "word/vbaProject.bin" in names:
                raise UnsupportedFileType("Macro-enabled documents are not accepted.")
            content_types = archive.open("[Content_Types].xml").read(MAX_CONTENT_TYPES_BYTES)
            if b"macroEnabled" in content_types:
                raise UnsupportedFileType("Macro-enabled documents are not accepted.")
    except zipfile.BadZipFile as exc:
        raise UnsupportedFileType("The DOCX could not be read; it may be corrupt.") from exc


def probe_structure(path: Path, extension: str, *, max_pages: int) -> StructureInfo:
    if extension == "pdf":
        return StructureInfo(page_count=probe_pdf(path, max_pages=max_pages))
    probe_docx(path)
    return StructureInfo(page_count=None)
