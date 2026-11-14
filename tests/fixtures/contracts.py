"""Builders for upload fixtures. Files are generated, not committed, so each invalid case is
explicit about what makes it invalid. PDFs come from pypdf, so they are structurally genuine."""

import io
import zipfile

from pypdf import PdfWriter

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
)


def document_xml(text: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )


def make_pdf(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_encrypted_pdf(user_password: str = "secret", owner_password: str = "owner") -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt(user_password=user_password, owner_password=owner_password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_docx(text: str = "This Agreement is made between the parties.") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/document.xml", document_xml(text))
    return buffer.getvalue()


def make_docx_with_macros() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/document.xml", document_xml("macro"))
        archive.writestr("word/vbaProject.bin", b"\xd0\xcf\x11\xe0 fake vba project")
    return buffer.getvalue()


def make_docm_content_type() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            CONTENT_TYPES.replace("document.main+xml", "document.macroEnabled.main+xml"),
        )
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/document.xml", document_xml("macro"))
    return buffer.getvalue()


def make_zip_bomb_docx(uncompressed_mib: int = 150) -> bytes:
    """A valid-looking DOCX whose one part decompresses to `uncompressed_mib` of zeros."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/document.xml", document_xml("bomb"))
        with archive.open("word/padding.bin", "w", force_zip64=True) as part:
            chunk = b"\0" * (1024 * 1024)
            for _ in range(uncompressed_mib):
                part.write(chunk)
    return buffer.getvalue()


def make_docx_missing_document_part() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("readme.txt", "just a zip")
    return buffer.getvalue()


def make_corrupt_pdf() -> bytes:
    return b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 99 0 R >>\nendobj\ngarbage garbage\n"


def make_corrupt_docx() -> bytes:
    return make_docx()[:200]


WINDOWS_EXECUTABLE = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + b"\x00" * 200
PLAIN_TEXT = b"This is just a text file pretending to be a contract.\n" * 20
PNG_IMAGE = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
