"""Upload a real document and run the real task with real Docling. Run with `pytest -m slow`."""

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from covenantiq.ingestion.extraction.docling_extractor import DoclingExtractor
from tests.fixtures import documents
from tests.integration.api.test_worker_flow import run_with

pytestmark = pytest.mark.slow

MakeSession = Callable[[], Any]
_shared: dict[str, DoclingExtractor] = {}


def real_extractor() -> DoclingExtractor:
    """One warmed-up extractor for the whole session (loading the models takes minutes)."""
    if "extractor" not in _shared:
        extractor = DoclingExtractor(page_batch=50)
        extractor.warm_up()
        _shared["extractor"] = extractor
    return _shared["extractor"]


async def upload(client: httpx.AsyncClient, session: Any, data: bytes, name: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/documents/upload", headers=session.headers, files={"file": (name, data)}
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def detail(client: httpx.AsyncClient, session: Any, document_id: str) -> dict[str, Any]:
    body: dict[str, Any] = (
        await client.get(f"/api/v1/documents/{document_id}", headers=session.headers)
    ).json()
    return body


async def test_a_real_msa_is_extracted_and_its_quality_stored(
    client: httpx.AsyncClient, make_session: MakeSession
) -> None:
    extractor = await asyncio.to_thread(real_extractor)
    session = await make_session()
    created = await upload(client, session, documents.make_msa_pdf(), "msa.pdf")
    job_id, document_id = created["job"]["id"], created["document"]["id"]

    result = await run_with(extractor, "ingest.process_document", job_id, request_id="req-slow")

    assert result.state == "SUCCESS"
    job = (await client.get(f"/api/v1/jobs/{job_id}", headers=session.headers)).json()
    assert (job["status"], job["stage"]) == ("succeeded", "chunking")
    document = await detail(client, session, document_id)
    assert document["status"] == "chunking"
    quality = document["extraction_quality"]
    assert (quality["pages_total"], quality["pages_with_text"]) == (5, 5)
    assert quality["tables"] == 1
    assert quality["headings"] >= 10
    assert quality["insufficient_text"] is False
    assert quality["docling_version"]
    assert document["metadata"] == {}


async def test_a_scanned_pdf_ends_failed_with_no_extractable_text(
    client: httpx.AsyncClient, make_session: MakeSession
) -> None:
    extractor = await asyncio.to_thread(real_extractor)
    session = await make_session()
    created = await upload(client, session, documents.make_scanned_pdf(), "scan.pdf")
    job_id, document_id = created["job"]["id"], created["document"]["id"]

    result = await run_with(extractor, "ingest.process_document", job_id)

    assert result.state == "REJECTED"
    job = (await client.get(f"/api/v1/jobs/{job_id}", headers=session.headers)).json()
    assert (job["status"], job["error"]["code"]) == ("failed", "NO_EXTRACTABLE_TEXT")
    document = await detail(client, session, document_id)
    assert (document["status"], document["error_code"]) == ("failed", "NO_EXTRACTABLE_TEXT")


async def test_an_injection_attempt_is_flagged_on_the_document(
    client: httpx.AsyncClient, make_session: MakeSession
) -> None:
    extractor = await asyncio.to_thread(real_extractor)
    session = await make_session()
    created = await upload(client, session, documents.make_injection_pdf(), "inject.pdf")

    result = await run_with(extractor, "ingest.process_document", created["job"]["id"])

    assert result.state == "SUCCESS"
    document = await detail(client, session, created["document"]["id"])
    assert document["status"] == "chunking"  # flagged text is still usable
    assert document["metadata"]["injection_warning"] is True
    assert "ignore_instructions" in document["metadata"]["injection_flags"]


async def test_a_docx_is_extracted_without_page_numbers(
    client: httpx.AsyncClient, make_session: MakeSession
) -> None:
    extractor = DoclingExtractor(page_batch=50)  # DOCX needs no ML models, so no warm-up
    session = await make_session()
    created = await upload(client, session, documents.make_msa_docx(), "msa.docx")

    await run_with(extractor, "ingest.process_document", created["job"]["id"])

    document = await detail(client, session, created["document"]["id"])
    assert document["status"] == "chunking"
    assert document["page_count"] is None
    assert document["extraction_quality"]["pages_total"] is None
    assert document["extraction_quality"]["headings"] >= 10
