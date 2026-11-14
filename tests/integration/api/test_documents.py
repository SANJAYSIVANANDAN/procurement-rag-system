import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from covenantiq.core.enums import DocumentStatus
from covenantiq.db.uow import UnitOfWork
from tests.fakes import RecordingDispatcher
from tests.fixtures import contracts as fixtures
from tests.fixtures import documents
from tests.integration.api.conftest import Session, bearer

UPLOAD = "/api/v1/documents/upload"
DOCUMENTS = "/api/v1/documents"
MakeSession = Callable[[], Any]

PDF = fixtures.make_pdf(3)


async def upload(
    client: httpx.AsyncClient,
    session: Session,
    data: bytes = PDF,
    filename: str = "acme-msa.pdf",
    **fields: str,
) -> httpx.Response:
    return await client.post(
        UPLOAD,
        headers=session.headers,
        files={"file": (filename, data, "application/octet-stream")},
        data=fields,
    )


def unique_pdf(pages: int = 1) -> bytes:
    """A valid PDF with distinct bytes, so it never collides with another upload."""
    return fixtures.make_pdf(pages) + b"\n% " + uuid4().hex.encode()


def stored_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()] if root.exists() else []


async def count_rows(uow_factory: Callable[[], UnitOfWork], table: str) -> int:
    async with uow_factory() as uow:
        result = await uow.session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
        return int(result.scalar_one())


def error_of(response: httpx.Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


class TestUploadHappyPath:
    async def test_pdf_is_accepted_stored_and_recorded(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        dispatcher: RecordingDispatcher,
        storage_root: Path,
    ) -> None:
        session = await make_session()
        response = await upload(client, session)

        assert response.status_code == 202
        body = response.json()
        assert body["duplicate"] is False
        document, job = body["document"], body["job"]
        assert document["status"] == "queued"
        assert document["title"] == "acme-msa"
        assert document["original_filename"] == "acme-msa.pdf"
        assert document["size_bytes"] == len(PDF)
        assert document["page_count"] == 3
        assert document["contract_type"] == "unknown"
        assert document["vendor_id"] is None
        assert (job["kind"], job["status"], job["progress"]) == ("document_processing", "queued", 0)
        assert response.headers["Location"] == f"/api/v1/jobs/{job['id']}"

        # exactly one file, at owner/document/sha.ext, with the uploaded bytes
        (stored,) = stored_files(storage_root)
        relative = stored.relative_to(storage_root).as_posix()
        assert relative.startswith(f"{session.id}/{document['id']}/")
        assert relative.endswith(".pdf")
        assert stored.read_bytes() == PDF
        assert not list((storage_root / ".tmp").iterdir())  # no temp files left behind
        assert dispatcher.ingestion == [UUID(job["id"])]

    async def test_docx_is_accepted(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        response = await upload(client, session, fixtures.make_docx(), "terms.DOCX")
        assert response.status_code == 202
        document = response.json()["document"]
        assert document["page_count"] is None
        assert document["title"] == "terms"

    async def test_a_docx_from_a_real_word_library_is_accepted(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        # Larger than the sniff window, with docProps parts: what real Word files look like.
        session = await make_session()
        response = await upload(client, session, documents.make_msa_docx(), "real-msa.docx")
        assert response.status_code == 202, response.text
        assert response.json()["document"]["page_count"] is None

    async def test_a_realistic_docx_with_a_macro_project_is_still_refused(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        import io
        import zipfile

        buffer = io.BytesIO(documents.make_msa_docx())
        with zipfile.ZipFile(buffer, "a") as archive:
            archive.writestr("word/vbaProject.bin", b"\xd0\xcf\x11\xe0 fake vba")
        session = await make_session()
        response = await upload(client, session, buffer.getvalue(), "macros.docx")
        assert response.status_code == 400
        assert error_of(response)["code"] == "UNSUPPORTED_FILE_TYPE"

    async def test_optional_metadata_is_applied(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        vendor = (
            await client.post("/api/v1/vendors", headers=session.headers, json={"name": "Acme"})
        ).json()
        response = await upload(
            client,
            session,
            title="  Acme   Cloud MSA 2026 ",
            vendor_id=vendor["id"],
            contract_type="msa",
        )
        assert response.status_code == 202
        document = response.json()["document"]
        assert document["title"] == "Acme Cloud MSA 2026"
        assert document["vendor_id"] == vendor["id"]
        assert document["contract_type"] == "msa"

    async def test_the_original_filename_is_sanitised_and_never_used_in_paths(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        storage_root: Path,
    ) -> None:
        session = await make_session()
        response = await upload(client, session, unique_pdf(), "../../etc/evil.pdf")
        assert response.status_code == 202
        assert response.json()["document"]["original_filename"] == "evil.pdf"
        (stored,) = stored_files(storage_root)
        assert "evil" not in stored.relative_to(storage_root).as_posix()


class TestUploadRejections:
    @pytest.mark.parametrize(
        ("filename", "data", "status", "code"),
        [
            ("virus.exe", fixtures.WINDOWS_EXECUTABLE, 400, "UNSUPPORTED_FILE_TYPE"),
            ("notes.txt", fixtures.PLAIN_TEXT, 400, "UNSUPPORTED_FILE_TYPE"),
            ("archive", fixtures.make_pdf(), 400, "UNSUPPORTED_FILE_TYPE"),
            ("macro.docm", fixtures.make_docx(), 400, "UNSUPPORTED_FILE_TYPE"),
            ("fake.pdf", fixtures.WINDOWS_EXECUTABLE, 400, "UNSUPPORTED_FILE_TYPE"),
            ("fake.pdf", fixtures.PLAIN_TEXT, 400, "UNSUPPORTED_FILE_TYPE"),
            ("fake.docx", fixtures.PNG_IMAGE, 400, "UNSUPPORTED_FILE_TYPE"),
            ("swapped.docx", fixtures.make_pdf(), 400, "UNSUPPORTED_FILE_TYPE"),
            ("swapped.pdf", fixtures.make_docx(), 400, "UNSUPPORTED_FILE_TYPE"),
            ("corrupt.pdf", fixtures.make_corrupt_pdf(), 400, "UNSUPPORTED_FILE_TYPE"),
            ("corrupt.docx", fixtures.make_corrupt_docx(), 400, "UNSUPPORTED_FILE_TYPE"),
            (
                "hollow.docx",
                fixtures.make_docx_missing_document_part(),
                400,
                "UNSUPPORTED_FILE_TYPE",
            ),
            ("macros.docx", fixtures.make_docx_with_macros(), 400, "UNSUPPORTED_FILE_TYPE"),
            ("macros2.docx", fixtures.make_docm_content_type(), 400, "UNSUPPORTED_FILE_TYPE"),
            ("empty.pdf", b"", 400, "UNSUPPORTED_FILE_TYPE"),
            ("locked.pdf", fixtures.make_encrypted_pdf("pw", "owner"), 400, "ENCRYPTED_PDF"),
            ("locked2.pdf", fixtures.make_encrypted_pdf("", "owner"), 400, "ENCRYPTED_PDF"),
            ("huge.pdf", fixtures.make_pdf(301), 400, "TOO_MANY_PAGES"),
            ("bomb.docx", fixtures.make_zip_bomb_docx(150), 413, "FILE_TOO_LARGE"),
        ],
    )
    async def test_invalid_files_get_the_right_code_and_leave_nothing_behind(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        dispatcher: RecordingDispatcher,
        storage_root: Path,
        uow_factory: Callable[[], UnitOfWork],
        filename: str,
        data: bytes,
        status: int,
        code: str,
    ) -> None:
        session = await make_session()
        response = await upload(client, session, data, filename)
        assert response.status_code == status, response.text
        assert error_of(response)["code"] == code
        assert error_of(response)["request_id"] == response.headers["X-Request-ID"]

        assert stored_files(storage_root) == []
        assert await count_rows(uow_factory, "documents") == 0
        assert await count_rows(uow_factory, "document_processing_jobs") == 0
        assert dispatcher.ingestion == []

    async def test_missing_file_field_is_a_422(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        response = await client.post(UPLOAD, headers=session.headers, data={"title": "x"})
        assert response.status_code == 422
        assert error_of(response)["code"] == "VALIDATION_ERROR"

    async def test_unknown_contract_type_is_a_422(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        response = await upload(client, session, contract_type="treaty")
        assert response.status_code == 422

    async def test_unauthenticated_upload_is_refused_before_anything_is_stored(
        self, client: httpx.AsyncClient, storage_root: Path
    ) -> None:
        response = await client.post(UPLOAD, files={"file": ("a.pdf", PDF)})
        assert response.status_code == 401
        assert stored_files(storage_root) == []


class TestUploadSizeLimits:
    async def test_a_26_mb_upload_is_cut_off_with_413_without_buffering_it(
        self, client: httpx.AsyncClient, make_session: MakeSession, storage_root: Path
    ) -> None:
        session = await make_session()
        total = 26 * 1024 * 1024
        chunk = 64 * 1024
        sent = 0

        async def body() -> AsyncIterator[bytes]:
            nonlocal sent
            yield (
                b"--b\r\nContent-Disposition: form-data; name=file; filename=big.pdf\r\n"
                b"Content-Type: application/pdf\r\n\r\n"
            )
            while sent < total:
                sent += chunk
                yield b"%PDF-1.7 " + b"x" * (chunk - 9)

        response = await client.post(
            UPLOAD,
            headers={
                **session.headers,
                "Content-Type": "multipart/form-data; boundary=b",
            },
            content=body(),  # chunked: no Content-Length to check up front
        )
        assert response.status_code == 413
        assert error_of(response)["code"] == "FILE_TOO_LARGE"
        # the server stopped reading soon after the 25 MB limit instead of taking all 26 MB
        assert sent < total
        assert stored_files(storage_root) == []

    async def test_a_26_mb_upload_with_a_declared_length_is_refused_outright(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        response = await upload(client, session, b"%PDF-1.7 " + b"x" * (26 * 1024 * 1024))
        assert response.status_code == 413
        assert error_of(response)["code"] == "FILE_TOO_LARGE"


class TestSmallUploadLimit:
    """The same rules with a 1 MB limit, so each boundary can be hit cheaply."""

    @pytest.fixture
    def max_upload_mb(self) -> int:
        return 1

    async def test_the_exact_limit_is_enforced_by_the_service(
        self, client: httpx.AsyncClient, make_session: MakeSession, storage_root: Path
    ) -> None:
        session = await make_session()
        # 10 KiB over the limit still fits the multipart allowance, so only the service refuses it
        response = await upload(client, session, b"%PDF-1.7 " + b"x" * (1024 * 1024 + 10 * 1024))
        assert response.status_code == 413
        assert error_of(response)["details"] == {"max_bytes": 1024 * 1024}
        assert stored_files(storage_root) == []

    async def test_gross_oversize_never_reaches_the_service(
        self, client: httpx.AsyncClient, make_session: MakeSession, storage_root: Path
    ) -> None:
        session = await make_session()
        response = await upload(client, session, b"x" * (3 * 1024 * 1024))
        assert response.status_code == 413
        assert stored_files(storage_root) == []

    async def test_a_file_just_under_the_limit_is_accepted(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        data = fixtures.make_pdf(1) + b"\n%" + b"x" * (900 * 1024)
        assert (await upload(client, session, data)).status_code == 202


class TestDuplicates:
    async def test_the_same_file_twice_returns_the_existing_document(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        dispatcher: RecordingDispatcher,
        storage_root: Path,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        session = await make_session()
        first = await upload(client, session)
        second = await upload(client, session, filename="renamed-copy.pdf")

        assert first.status_code == 202
        assert second.status_code == 200
        assert second.json()["duplicate"] is True
        assert second.json()["document"]["id"] == first.json()["document"]["id"]
        assert second.json()["document"]["original_filename"] == "acme-msa.pdf"
        assert second.json()["job"]["id"] == first.json()["job"]["id"]
        assert await count_rows(uow_factory, "documents") == 1
        assert len(stored_files(storage_root)) == 1
        assert len(dispatcher.ingestion) == 1
        assert "Location" not in second.headers

    async def test_different_users_uploading_the_same_bytes_get_separate_documents(
        self, client: httpx.AsyncClient, make_session: MakeSession, storage_root: Path
    ) -> None:
        alice, bob = await make_session(), await make_session()
        a = await upload(client, alice)
        b = await upload(client, bob)
        assert (a.status_code, b.status_code) == (202, 202)
        assert a.json()["document"]["id"] != b.json()["document"]["id"]
        assert len(stored_files(storage_root)) == 2

    async def test_concurrent_identical_uploads_create_exactly_one_document(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        dispatcher: RecordingDispatcher,
        storage_root: Path,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        session = await make_session()
        data = unique_pdf(2)
        responses = await asyncio.gather(*(upload(client, session, data) for _ in range(8)))

        assert sorted(r.status_code for r in responses) == [200] * 7 + [202]
        assert len({r.json()["document"]["id"] for r in responses}) == 1
        assert await count_rows(uow_factory, "documents") == 1
        assert await count_rows(uow_factory, "document_processing_jobs") == 1
        assert len(stored_files(storage_root)) == 1  # the losers' files were cleaned up
        assert not list((storage_root / ".tmp").iterdir())
        assert len(dispatcher.ingestion) == 1

    async def test_after_deletion_the_same_bytes_can_be_uploaded_again(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        first = (await upload(client, session)).json()["document"]
        assert (
            await client.delete(f"{DOCUMENTS}/{first['id']}", headers=session.headers)
        ).status_code == 204
        again = await upload(client, session)
        assert again.status_code == 202
        assert again.json()["document"]["id"] != first["id"]


class TestUploadGuards:
    @pytest.fixture
    def max_active_jobs(self) -> int:
        return 2

    async def test_too_many_active_jobs_is_a_429(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        assert (await upload(client, session, unique_pdf())).status_code == 202
        assert (await upload(client, session, unique_pdf())).status_code == 202
        third = await upload(client, session, unique_pdf())
        assert third.status_code == 429
        assert error_of(third)["code"] == "TOO_MANY_ACTIVE_JOBS"

        other = await make_session()  # the limit is per user
        assert (await upload(client, other, unique_pdf())).status_code == 202


class TestUploadRateLimit:
    async def test_upload_bucket_limits_per_user(
        self, client: httpx.AsyncClient, make_session: MakeSession, container: Any
    ) -> None:
        from covenantiq.services.rate_limiter import RateLimit

        container.rate_limiter._limits["upload"] = RateLimit(2, 3600)
        session = await make_session()
        for _ in range(2):
            assert (await upload(client, session, unique_pdf())).status_code == 202
        third = await upload(client, session, unique_pdf())
        assert third.status_code == 429
        assert error_of(third)["code"] == "RATE_LIMITED"
        assert int(third.headers["Retry-After"]) > 0


class TestVendorLink:
    async def test_unknown_vendor_is_a_404_and_nothing_is_stored(
        self, client: httpx.AsyncClient, make_session: MakeSession, storage_root: Path
    ) -> None:
        session = await make_session()
        response = await upload(client, session, vendor_id=str(uuid4()))
        assert response.status_code == 404
        assert error_of(response)["code"] == "NOT_FOUND"
        assert stored_files(storage_root) == []

    async def test_another_users_vendor_is_a_404(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        alice, bob = await make_session(), await make_session()
        vendor = (
            await client.post("/api/v1/vendors", headers=alice.headers, json={"name": "Acme"})
        ).json()
        response = await upload(client, bob, vendor_id=vendor["id"])
        assert response.status_code == 404


class TestDispatchFailure:
    async def test_a_dispatch_failure_marks_the_job_and_document_failed(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        dispatcher: RecordingDispatcher,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        dispatcher.fail = True
        session = await make_session()
        response = await upload(client, session)
        assert response.status_code == 503
        assert error_of(response)["code"] == "DISPATCH_FAILED"

        async with uow_factory() as uow:
            row = (
                await uow.session.execute(
                    text(
                        "SELECT d.status, d.error_code, j.status, j.error_code "
                        "FROM documents d JOIN document_processing_jobs j ON j.document_id = d.id"
                    )
                )
            ).one()
        assert tuple(row) == ("failed", "DISPATCH_FAILED", "failed", "DISPATCH_FAILED")


class TestListAndGet:
    async def test_list_returns_only_own_documents_newest_first(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        alice, bob = await make_session(), await make_session()
        ids = [
            (await upload(client, alice, unique_pdf())).json()["document"]["id"] for _ in range(3)
        ]
        await upload(client, bob, unique_pdf())

        response = await client.get(DOCUMENTS, headers=alice.headers)
        assert response.status_code == 200
        body = response.json()
        assert [d["id"] for d in body["items"]] == list(reversed(ids))
        assert body["next_cursor"] is None

    async def test_cursor_pagination_walks_every_document_once(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        ids = [
            (await upload(client, session, unique_pdf())).json()["document"]["id"] for _ in range(5)
        ]

        seen: list[str] = []
        cursor: str | None = None
        page_sizes: list[int] = []
        while True:
            params: dict[str, Any] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            body = (await client.get(DOCUMENTS, headers=session.headers, params=params)).json()
            page_sizes.append(len(body["items"]))
            seen += [d["id"] for d in body["items"]]
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert page_sizes == [2, 2, 1]
        assert seen == list(reversed(ids))

    async def test_filters(self, client: httpx.AsyncClient, make_session: MakeSession) -> None:
        session = await make_session()
        vendor = (
            await client.post("/api/v1/vendors", headers=session.headers, json={"name": "Acme"})
        ).json()
        linked = (
            await upload(client, session, unique_pdf(), title="Acme MSA", vendor_id=vendor["id"])
        ).json()["document"]
        other = (await upload(client, session, unique_pdf(), title="Globex NDA")).json()["document"]

        async def ids(**params: Any) -> list[str]:
            response = await client.get(DOCUMENTS, headers=session.headers, params=params)
            assert response.status_code == 200
            return [d["id"] for d in response.json()["items"]]

        assert await ids(q="acme") == [linked["id"]]
        assert await ids(vendor_id=vendor["id"]) == [linked["id"]]
        assert set(await ids(status="queued")) == {linked["id"], other["id"]}
        assert await ids(status="ready") == []
        assert await ids(q="100%") == []  # wildcards are literal

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"status": "bogus"}])
    async def test_invalid_query_parameters_are_422(
        self, client: httpx.AsyncClient, make_session: MakeSession, params: dict[str, Any]
    ) -> None:
        session = await make_session()
        response = await client.get(DOCUMENTS, headers=session.headers, params=params)
        assert response.status_code == 422

    @pytest.mark.parametrize("cursor", ["garbage", "e30", "W10", "W1wiYVwiLFwiYlwiXQ"])
    async def test_a_tampered_cursor_is_a_400(
        self, client: httpx.AsyncClient, make_session: MakeSession, cursor: str
    ) -> None:
        session = await make_session()
        response = await client.get(DOCUMENTS, headers=session.headers, params={"cursor": cursor})
        assert response.status_code == 400
        assert error_of(response)["code"] == "VALIDATION_ERROR"

    async def test_detail_includes_the_latest_job_and_quality(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        created = (await upload(client, session)).json()
        response = await client.get(
            f"{DOCUMENTS}/{created['document']['id']}", headers=session.headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == created["document"]["id"]
        assert body["job"]["id"] == created["job"]["id"]
        assert body["extraction_quality"] == {}
        assert body["error_code"] is None

    async def test_unknown_and_malformed_ids(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        unknown = await client.get(f"{DOCUMENTS}/{uuid4()}", headers=session.headers)
        assert unknown.status_code == 404
        assert error_of(unknown)["code"] == "NOT_FOUND"
        malformed = await client.get(f"{DOCUMENTS}/not-a-uuid", headers=session.headers)
        assert malformed.status_code == 422


class TestStatus:
    async def test_status_reflects_progress(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        session = await make_session()
        document_id = (await upload(client, session)).json()["document"]["id"]
        url = f"{DOCUMENTS}/{document_id}/status"

        queued = (await client.get(url, headers=session.headers)).json()
        assert queued["status"] == "queued"
        assert queued["progress"] == 0
        assert queued["page_count"] == 3
        assert queued["error"] is None

        async with uow_factory() as uow:
            await uow.documents.system_set_status(
                UUID(document_id), [DocumentStatus.QUEUED], DocumentStatus.READY, chunk_count=40
            )
            await uow.commit()
        ready = (await client.get(url, headers=session.headers)).json()
        assert (ready["status"], ready["progress"], ready["chunk_count"]) == ("ready", 1.0, 40)


class TestUpdate:
    async def test_patch_changes_only_what_was_sent(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        vendor = (
            await client.post("/api/v1/vendors", headers=session.headers, json={"name": "Acme"})
        ).json()
        document = (await upload(client, session)).json()["document"]
        url = f"{DOCUMENTS}/{document['id']}"

        response = await client.patch(
            url,
            headers=session.headers,
            json={
                "title": "  New   title ",
                "vendor_id": vendor["id"],
                "contract_type": "saas",
                "effective_date": "2026-01-01",
                "expiry_date": "2027-01-01",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "New title"
        assert body["vendor_id"] == vendor["id"]
        assert body["contract_type"] == "saas"
        assert (body["effective_date"], body["expiry_date"]) == ("2026-01-01", "2027-01-01")
        assert body["original_filename"] == "acme-msa.pdf"

        again = await client.patch(url, headers=session.headers, json={"contract_type": "dpa"})
        assert again.json()["title"] == "New title"  # untouched
        assert again.json()["contract_type"] == "dpa"

        unlinked = await client.patch(url, headers=session.headers, json={"vendor_id": None})
        assert unlinked.json()["vendor_id"] is None

    @pytest.mark.parametrize(
        ("payload", "status"),
        [
            ({"expiry_date": "2025-01-01", "effective_date": "2026-01-01"}, 400),
            ({"title": "   "}, 400),
            ({"title": None}, 400),
            ({"contract_type": None}, 400),
            ({"title": "x" * 256}, 422),
            ({"status": "ready"}, 422),
            ({"size_bytes": 1}, 422),
            ({"contract_type": "treaty"}, 422),
            ({"effective_date": "not-a-date"}, 422),
        ],
    )
    async def test_invalid_patches(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        payload: dict[str, Any],
        status: int,
    ) -> None:
        session = await make_session()
        document = (await upload(client, session)).json()["document"]
        response = await client.patch(
            f"{DOCUMENTS}/{document['id']}", headers=session.headers, json=payload
        )
        assert response.status_code == status, response.text

    async def test_the_new_expiry_is_checked_against_the_stored_effective_date(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        session = await make_session()
        document = (await upload(client, session)).json()["document"]
        url = f"{DOCUMENTS}/{document['id']}"
        await client.patch(url, headers=session.headers, json={"effective_date": "2026-06-01"})
        response = await client.patch(
            url, headers=session.headers, json={"expiry_date": "2026-01-01"}
        )
        assert response.status_code == 400

    async def test_linking_a_vendor_that_is_not_yours_is_a_404(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        alice, bob = await make_session(), await make_session()
        vendor = (
            await client.post("/api/v1/vendors", headers=alice.headers, json={"name": "Acme"})
        ).json()
        document = (await upload(client, bob)).json()["document"]
        response = await client.patch(
            f"{DOCUMENTS}/{document['id']}", headers=bob.headers, json={"vendor_id": vendor["id"]}
        )
        assert response.status_code == 404


class TestDelete:
    async def test_delete_hides_the_document_and_requests_a_purge(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        dispatcher: RecordingDispatcher,
    ) -> None:
        session = await make_session()
        document_id = (await upload(client, session)).json()["document"]["id"]
        url = f"{DOCUMENTS}/{document_id}"

        response = await client.delete(url, headers=session.headers)
        assert response.status_code == 204
        assert response.content == b""
        assert dispatcher.purges == [UUID(document_id)]

        assert (await client.get(url, headers=session.headers)).status_code == 404
        assert (await client.get(f"{url}/status", headers=session.headers)).status_code == 404
        assert (await client.get(DOCUMENTS, headers=session.headers)).json()["items"] == []
        assert (await client.delete(url, headers=session.headers)).status_code == 404

    async def test_a_failing_purge_dispatch_does_not_fail_the_delete(
        self,
        client: httpx.AsyncClient,
        make_session: MakeSession,
        dispatcher: RecordingDispatcher,
    ) -> None:
        session = await make_session()
        document_id = (await upload(client, session)).json()["document"]["id"]
        dispatcher.fail = True
        response = await client.delete(f"{DOCUMENTS}/{document_id}", headers=session.headers)
        assert response.status_code == 204


class TestOwnership:
    async def test_user_b_gets_404_for_user_as_documents_everywhere(
        self, client: httpx.AsyncClient, make_session: MakeSession
    ) -> None:
        alice, bob = await make_session(), await make_session()
        document = (await upload(client, alice)).json()["document"]
        url = f"{DOCUMENTS}/{document['id']}"

        for method, path, kwargs in [
            ("GET", url, {}),
            ("GET", f"{url}/status", {}),
            ("PATCH", url, {"json": {"title": "hijacked"}}),
            ("DELETE", url, {}),
        ]:
            response = await client.request(method, path, headers=bob.headers, **kwargs)
            assert response.status_code == 404, (method, path)
            assert error_of(response)["code"] == "NOT_FOUND"
            # indistinguishable from a document that never existed
            missing = await client.request(
                method, path.replace(document["id"], str(uuid4())), headers=bob.headers, **kwargs
            )
            assert error_of(response)["message"] == error_of(missing)["message"]

        assert (await client.get(DOCUMENTS, headers=bob.headers)).json()["items"] == []
        untouched = (await client.get(url, headers=alice.headers)).json()
        assert untouched["title"] == "acme-msa"

    async def test_every_endpoint_requires_authentication(self, client: httpx.AsyncClient) -> None:
        some_id = uuid4()
        for method, path in [
            ("GET", DOCUMENTS),
            ("GET", f"{DOCUMENTS}/{some_id}"),
            ("GET", f"{DOCUMENTS}/{some_id}/status"),
            ("PATCH", f"{DOCUMENTS}/{some_id}"),
            ("DELETE", f"{DOCUMENTS}/{some_id}"),
        ]:
            response = await client.request(method, path, json={} if method == "PATCH" else None)
            assert response.status_code == 401, (method, path)
        bad = await client.get(DOCUMENTS, headers=bearer("garbage"))
        assert bad.status_code == 401
