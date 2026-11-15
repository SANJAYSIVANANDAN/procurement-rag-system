"""The only way the UI reaches anything: typed wrappers around the CovenantIQ HTTP API (spec 27.1).

Nothing here imports Streamlit, so the client is tested on its own. A method returns the decoded
JSON the API sent (documented per method), raises `ApiError` for an error body, and `Unauthorized`
(a subclass) for a 401, which the UI answers by clearing the token and showing the login page.
"""

from __future__ import annotations

import contextlib
import mimetypes
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import httpx
from httpx_sse import EventSource, connect_sse

JSON = dict[str, Any]
STREAM_TIMEOUT = httpx.Timeout(10.0, read=None)  # a reply may be quiet for a while


class ApiError(Exception):
    """An error the API reported: its `code`, a message safe to show, and the request id."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.details = details


class Unauthorized(ApiError):
    """The token is missing, expired or wrong."""


class Unreachable(ApiError):
    """The API did not answer (network down, wrong URL, still starting)."""

    def __init__(self) -> None:
        super().__init__(0, "API_UNREACHABLE", "The CovenantIQ API cannot be reached right now.")


def _error_from(response: httpx.Response) -> ApiError:
    code, message, request_id, details = (
        "HTTP_ERROR",
        f"Request failed ({response.status_code}).",
        None,
        None,
    )
    with contextlib.suppress(ValueError):
        body = response.json().get("error", {})
        code = body.get("code", code)
        message = body.get("message", message)
        request_id = body.get("request_id")
        details = body.get("details")
    request_id = request_id or response.headers.get("x-request-id")
    kind = Unauthorized if response.status_code == 401 else ApiError
    return kind(response.status_code, code, message, request_id=request_id, details=details)


class ApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.token = token
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, read=max(timeout, 60.0)),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    # ---- plumbing ---------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
        ok_empty: bool = False,
    ) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        try:
            response = self._http.request(
                method,
                path,
                params=clean or None,
                json=json,
                data=data,
                files=files,
                headers=self._headers(),
            )
        except httpx.TransportError as exc:
            raise Unreachable() from exc
        if response.status_code >= 400:
            raise _error_from(response)
        if ok_empty or response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ---- auth -------------------------------------------------------------------------------

    def register(self, email: str, password: str, full_name: str | None = None) -> JSON:
        body: JSON = {"email": email, "password": password}
        if full_name:
            body["full_name"] = full_name
        return self._request("POST", "/auth/register", json=body)  # type: ignore[no-any-return]

    def login(self, email: str, password: str) -> str:
        """The access token. It is also kept on the client for the calls that follow."""
        body = self._request("POST", "/auth/login", json={"email": email, "password": password})
        self.token = str(body["access_token"])
        return self.token

    def me(self) -> JSON:
        return self._request("GET", "/auth/me")  # type: ignore[no-any-return]

    # ---- documents --------------------------------------------------------------------------

    def upload_document(
        self,
        filename: str,
        content: bytes,
        *,
        title: str | None = None,
        vendor_id: str | None = None,
        contract_type: str | None = None,
    ) -> JSON:
        """`{document, job, duplicate}`."""
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        data = {
            k: v
            for k, v in {
                "title": title,
                "vendor_id": vendor_id,
                "contract_type": contract_type,
            }.items()
            if v
        }
        return self._request(  # type: ignore[no-any-return]
            "POST", "/documents/upload", data=data, files={"file": (filename, content, mime)}
        )

    def list_documents(
        self,
        *,
        status: str | None = None,
        vendor_id: str | None = None,
        q: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> JSON:
        """`{items, next_cursor}`."""
        params = {
            "status": status,
            "vendor_id": vendor_id,
            "q": q,
            "limit": limit,
            "cursor": cursor,
        }
        return self._request("GET", "/documents", params=params)  # type: ignore[no-any-return]

    def all_documents(self, *, max_items: int = 500, **filters: Any) -> list[JSON]:
        return self._all(self.list_documents, max_items, **filters)

    def get_document(self, document_id: str) -> JSON:
        return self._request("GET", f"/documents/{document_id}")  # type: ignore[no-any-return]

    def update_document(self, document_id: str, **fields: Any) -> JSON:
        return self._request("PATCH", f"/documents/{document_id}", json=fields)  # type: ignore[no-any-return]

    def delete_document(self, document_id: str) -> None:
        self._request("DELETE", f"/documents/{document_id}", ok_empty=True)

    def document_status(self, document_id: str) -> JSON:
        return self._request("GET", f"/documents/{document_id}/status")  # type: ignore[no-any-return]

    def analyze_document(self, document_id: str, *, force: bool = False) -> JSON:
        """`{job}`."""
        return self._request(  # type: ignore[no-any-return]
            "POST", f"/documents/{document_id}/analyze", json={"force": force}
        )

    def findings(
        self,
        document_id: str,
        *,
        job_id: str | None = None,
        severity: str | None = None,
        status: str | None = None,
        category: str | None = None,
    ) -> JSON:
        params = {"job_id": job_id, "severity": severity, "status": status, "category": category}
        return self._request(  # type: ignore[no-any-return]
            "GET", f"/documents/{document_id}/findings", params=params
        )

    def job(self, job_id: str) -> JSON:
        return self._request("GET", f"/jobs/{job_id}")  # type: ignore[no-any-return]

    # ---- conversations ----------------------------------------------------------------------

    def create_conversation(
        self,
        *,
        title: str | None = None,
        document_ids: list[str] | None = None,
        vendor_id: str | None = None,
    ) -> JSON:
        body = {"title": title, "document_ids": document_ids or [], "vendor_id": vendor_id}
        return self._request("POST", "/conversations", json=body)  # type: ignore[no-any-return]

    def list_conversations(self, *, archived: bool = False, limit: int = 50) -> JSON:
        params = {"archived": str(archived).lower(), "limit": limit}
        return self._request("GET", "/conversations", params=params)  # type: ignore[no-any-return]

    def get_conversation(self, conversation_id: str) -> JSON:
        return self._request("GET", f"/conversations/{conversation_id}")  # type: ignore[no-any-return]

    def update_conversation(self, conversation_id: str, **fields: Any) -> JSON:
        return self._request(  # type: ignore[no-any-return]
            "PATCH", f"/conversations/{conversation_id}", json=fields
        )

    def messages(
        self, conversation_id: str, *, before_seq: int | None = None, limit: int = 50
    ) -> JSON:
        """`{items (oldest first), next_cursor}`; the cursor is the `before_seq` of older ones."""
        params = {"before_seq": before_seq, "limit": limit}
        return self._request(  # type: ignore[no-any-return]
            "GET", f"/conversations/{conversation_id}/messages", params=params
        )

    def send_message(
        self,
        conversation_id: str,
        content: str,
        *,
        client_message_id: str | None = None,
        document_ids: list[str] | None = None,
    ) -> JSON:
        """`{user_message_id, assistant_message_id, stream_url, status}`. Sending the same
        `client_message_id` again returns the original ids and generates nothing."""
        body: JSON = {"content": content, "client_message_id": client_message_id or str(uuid4())}
        if document_ids:
            body["document_ids"] = document_ids
        return self._request(  # type: ignore[no-any-return]
            "POST", f"/conversations/{conversation_id}/messages", json=body
        )

    @contextlib.contextmanager
    def open_stream(
        self, conversation_id: str, message_id: str, last_event_id: str | None = None
    ) -> Iterator[EventSource]:
        """The reply's server-sent events. Raises `ApiError` before yielding if the API refuses."""
        headers = {**self._headers(), "Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        try:
            with connect_sse(
                self._http,
                "GET",
                f"/conversations/{conversation_id}/stream",
                params={"message_id": message_id},
                headers=headers,
                timeout=STREAM_TIMEOUT,
            ) as source:
                if source.response.status_code >= 400:
                    source.response.read()
                    raise _error_from(source.response)
                yield source
        except httpx.TransportError as exc:
            raise Unreachable() from exc

    # ---- vendors ----------------------------------------------------------------------------

    def create_vendor(
        self, name: str, *, industry: str | None = None, website: str | None = None
    ) -> JSON:
        body = {"name": name, "industry": industry or None, "website": website or None}
        return self._request("POST", "/vendors", json=body)  # type: ignore[no-any-return]

    def list_vendors(
        self, *, q: str | None = None, limit: int = 50, cursor: str | None = None
    ) -> JSON:
        params = {"q": q, "limit": limit, "cursor": cursor}
        return self._request("GET", "/vendors", params=params)  # type: ignore[no-any-return]

    def all_vendors(self, *, max_items: int = 500, **filters: Any) -> list[JSON]:
        return self._all(self.list_vendors, max_items, **filters)

    def get_vendor(self, vendor_id: str) -> JSON:
        return self._request("GET", f"/vendors/{vendor_id}")  # type: ignore[no-any-return]

    def update_vendor(self, vendor_id: str, **fields: Any) -> JSON:
        return self._request("PATCH", f"/vendors/{vendor_id}", json=fields)  # type: ignore[no-any-return]

    def assess_vendor(self, vendor_id: str) -> JSON:
        """`{job}` (with `pending_dependencies`: the contract analyses it waits for)."""
        return self._request("POST", f"/vendors/{vendor_id}/assess")  # type: ignore[no-any-return]

    def assessments(self, vendor_id: str, *, limit: int = 20, offset: int = 0) -> list[JSON]:
        params = {"limit": limit, "offset": offset}
        return self._request(  # type: ignore[no-any-return]
            "GET", f"/vendors/{vendor_id}/assessments", params=params
        )

    def latest_assessment(self, vendor_id: str) -> JSON | None:
        """The latest assessment, or None if the vendor has not been assessed."""
        try:
            return self._request("GET", f"/vendors/{vendor_id}/assessments/latest")  # type: ignore[no-any-return]
        except ApiError as error:
            if error.status == 404:
                return None
            raise

    def compare_vendors(self, vendor_ids: list[str], *, require_fresh: bool = True) -> JSON:
        """`{comparison_id, job}`."""
        body = {"vendor_ids": vendor_ids, "require_fresh": require_fresh}
        return self._request("POST", "/vendors/compare", json=body)  # type: ignore[no-any-return]

    def comparison(self, comparison_id: str) -> JSON:
        return self._request("GET", f"/vendor-comparisons/{comparison_id}")  # type: ignore[no-any-return]

    # ---- helpers ----------------------------------------------------------------------------

    @staticmethod
    def _all(fetch: Any, max_items: int, **filters: Any) -> list[JSON]:
        items: list[JSON] = []
        cursor: str | None = None
        while len(items) < max_items:
            page = fetch(limit=min(100, max_items - len(items)), cursor=cursor, **filters)
            items.extend(page["items"])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        return items
