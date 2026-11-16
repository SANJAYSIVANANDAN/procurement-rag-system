"""The UI's API client against a mocked HTTP API (respx)."""

import json

import httpx
import pytest
import respx

from frontend.api_client import ApiClient, ApiError, Unauthorized, Unreachable

BASE = "http://api.test/api/v1"
ID = "01a1dcfb-8d0a-7790-b29c-030dd3db8c8a"


@pytest.fixture
def api() -> ApiClient:
    return ApiClient(BASE, token="tok")


def error_body(
    code: str, message: str, status: int, request_id: str | None = "req-1"
) -> httpx.Response:
    body = {"error": {"code": code, "message": message, "details": {}, "request_id": request_id}}
    return httpx.Response(status, json=body)


class TestRequests:
    @respx.mock
    def test_the_token_is_sent_and_empty_filters_are_dropped(self, api: ApiClient) -> None:
        route = respx.get(f"{BASE}/documents").respond(json={"items": [], "next_cursor": None})

        api.list_documents(status="ready", vendor_id=None, q="", limit=10)

        request = route.calls.last.request
        assert request.headers["authorization"] == "Bearer tok"
        assert dict(request.url.params) == {"status": "ready", "limit": "10"}

    @respx.mock
    def test_login_keeps_the_token_for_the_next_calls(self) -> None:
        api = ApiClient(BASE)
        respx.post(f"{BASE}/auth/login").respond(json={"access_token": "abc", "expires_in": 60})
        me = respx.get(f"{BASE}/auth/me").respond(json={"id": ID, "email": "a@b.c"})

        assert api.login("a@b.c", "pw") == "abc"
        api.me()

        assert me.calls.last.request.headers["authorization"] == "Bearer abc"

    @respx.mock
    def test_an_upload_is_multipart_with_only_the_fields_that_were_given(
        self, api: ApiClient
    ) -> None:
        route = respx.post(f"{BASE}/documents/upload").respond(
            json={"document": {"id": ID}, "job": None, "duplicate": False}
        )

        api.upload_document("msa.pdf", b"%PDF-1.7", contract_type="msa", vendor_id=None)

        request = route.calls.last.request
        assert request.headers["content-type"].startswith("multipart/form-data")
        body = request.content
        assert b'name="file"; filename="msa.pdf"' in body and b"application/pdf" in body
        assert b'name="contract_type"' in body and b'name="vendor_id"' not in body

    @respx.mock
    def test_pages_are_followed_until_the_cursor_runs_out(self, api: ApiClient) -> None:
        respx.get(f"{BASE}/documents").mock(
            side_effect=[
                httpx.Response(
                    200, json={"items": [{"id": "1"}, {"id": "2"}], "next_cursor": "c1"}
                ),
                httpx.Response(200, json={"items": [{"id": "3"}], "next_cursor": None}),
            ]
        )
        assert [d["id"] for d in api.all_documents()] == ["1", "2", "3"]

    @respx.mock
    def test_sending_a_message_always_carries_a_client_message_id(self, api: ApiClient) -> None:
        route = respx.post(f"{BASE}/conversations/{ID}/messages").respond(
            status_code=202, json={"assistant_message_id": ID}
        )
        api.send_message(ID, "Is it capped?")
        sent = json.loads(route.calls.last.request.content)
        assert sent["content"] == "Is it capped?" and sent["client_message_id"]

    @respx.mock
    def test_no_assessment_yet_is_none_not_an_error(self, api: ApiClient) -> None:
        respx.get(f"{BASE}/vendors/{ID}/assessments/latest").mock(
            return_value=error_body("NOT_FOUND", "No assessment.", 404)
        )
        assert api.latest_assessment(ID) is None

    @respx.mock
    def test_a_delete_returns_nothing(self, api: ApiClient) -> None:
        respx.delete(f"{BASE}/documents/{ID}").respond(status_code=204)
        assert api.delete_document(ID) is None


class TestErrors:
    @respx.mock
    def test_an_api_error_carries_its_code_message_and_request_id(self, api: ApiClient) -> None:
        respx.post(f"{BASE}/conversations/{ID}/messages").mock(
            return_value=error_body("GENERATION_IN_PROGRESS", "A reply is being written.", 409)
        )
        with pytest.raises(ApiError) as caught:
            api.send_message(ID, "again")
        error = caught.value
        assert (error.status, error.code, error.request_id) == (
            409,
            "GENERATION_IN_PROGRESS",
            "req-1",
        )
        assert error.message == "A reply is being written."

    @respx.mock
    def test_a_401_is_unauthorized(self, api: ApiClient) -> None:
        respx.get(f"{BASE}/auth/me").mock(return_value=error_body("TOKEN_EXPIRED", "Expired.", 401))
        with pytest.raises(Unauthorized):
            api.me()

    @respx.mock
    def test_a_body_that_is_not_json_still_gives_an_error(self, api: ApiClient) -> None:
        respx.get(f"{BASE}/documents").respond(status_code=502, text="<html>bad gateway</html>")
        with pytest.raises(ApiError) as caught:
            api.list_documents()
        assert caught.value.status == 502 and caught.value.code == "HTTP_ERROR"

    @respx.mock
    def test_an_unreachable_api_is_reported_plainly(self, api: ApiClient) -> None:
        respx.get(f"{BASE}/documents").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(Unreachable) as caught:
            api.list_documents()
        assert caught.value.code == "API_UNREACHABLE"

    @respx.mock
    def test_validation_details_are_kept(self, api: ApiClient) -> None:
        body = {
            "error": {
                "code": "CONFLICT",
                "message": "m",
                "details": {"vendor_ids": ["v1"]},
            }
        }
        respx.post(f"{BASE}/vendors").respond(status_code=409, json=body)
        with pytest.raises(ApiError) as caught:
            api._request("POST", "/vendors", json={"name": "x"})
        assert caught.value.details == {"vendor_ids": ["v1"]}


class TestStreamRequest:
    @respx.mock
    def test_the_stream_is_requested_with_the_message_id_and_last_event_id(
        self, api: ApiClient
    ) -> None:
        route = respx.get(f"{BASE}/conversations/{ID}/stream").respond(
            headers={"content-type": "text/event-stream"}, content=b"event: x\ndata: {}\n\n"
        )
        with api.open_stream(ID, "m-1", "170-0") as source:
            assert [e.event for e in source.iter_sse()] == ["x"]
        request = route.calls.last.request
        assert request.url.params["message_id"] == "m-1"
        assert request.headers["last-event-id"] == "170-0"
        assert request.headers["accept"] == "text/event-stream"

    @respx.mock
    def test_a_refused_stream_raises_before_yielding(self, api: ApiClient) -> None:
        respx.get(f"{BASE}/conversations/{ID}/stream").mock(
            return_value=error_body("NOT_FOUND", "Message not found.", 404)
        )
        with pytest.raises(ApiError) as caught, api.open_stream(ID, "m-1"):
            pass
        assert caught.value.status == 404
