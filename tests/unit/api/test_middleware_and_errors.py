import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from covenantiq.api.errors import STATUS_BY_CODE, register_exception_handlers
from covenantiq.api.middleware import RequestContextMiddleware, resolve_request_id
from covenantiq.core.errors import (
    CovenantError,
    DocumentNotReady,
    ErrorCode,
    NotFound,
    RateLimited,
    TokenExpired,
    Unauthenticated,
)


class Body(BaseModel):
    password: str


def build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/not-found")
    async def not_found() -> None:
        raise NotFound("Document not found.", details={"document_id": "d1"})

    @app.get("/conflict")
    async def conflict() -> None:
        raise DocumentNotReady("Still processing.", details={"status": "embedding"})

    @app.get("/limited")
    async def limited() -> None:
        raise RateLimited("Slow down.", retry_after=42)

    @app.get("/expired")
    async def expired() -> None:
        raise TokenExpired("Expired.")

    @app.get("/unauthenticated")
    async def unauthenticated() -> None:
        raise Unauthenticated("Nope.")

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail: db password is hunter2")

    @app.post("/body")
    async def body(payload: Body) -> dict[str, str]:
        return {"status": "ok"}

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app(), raise_server_exceptions=False)


class TestRequestId:
    def test_generated_when_absent(self, client: TestClient) -> None:
        response = client.get("/ok")
        assert len(response.headers["X-Request-ID"]) == 32

    def test_supplied_id_is_echoed(self, client: TestClient) -> None:
        response = client.get("/ok", headers={"X-Request-ID": "trace-123.abc_DEF"})
        assert response.headers["X-Request-ID"] == "trace-123.abc_DEF"

    @pytest.mark.parametrize("bad", ["has space", "x" * 129, "semi;colon", "new\tline"])
    def test_malformed_ids_are_replaced(self, client: TestClient, bad: str) -> None:
        response = client.get("/ok", headers={"X-Request-ID": bad})
        assert response.headers["X-Request-ID"] != bad
        assert len(response.headers["X-Request-ID"]) == 32

    def test_resolve_request_id_is_pure(self) -> None:
        assert resolve_request_id("abc") == "abc"
        assert resolve_request_id(None) != resolve_request_id(None)

    def test_error_body_and_header_carry_the_same_id(self, client: TestClient) -> None:
        response = client.get("/not-found", headers={"X-Request-ID": "req-9"})
        assert response.headers["X-Request-ID"] == "req-9"
        assert response.json()["error"]["request_id"] == "req-9"


def test_security_headers_are_added(client: TestClient) -> None:
    response = client.get("/ok")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"


class TestErrorFormat:
    def test_domain_error_shape(self, client: TestClient) -> None:
        response = client.get("/conflict")
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "DOCUMENT_NOT_READY"
        assert error["message"] == "Still processing."
        assert error["details"] == {"status": "embedding"}
        assert set(error) == {"code", "message", "details", "request_id"}

    def test_not_found(self, client: TestClient) -> None:
        response = client.get("/not-found")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    def test_rate_limited_sets_retry_after(self, client: TestClient) -> None:
        response = client.get("/limited")
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "42"
        assert response.json()["error"]["code"] == "RATE_LIMITED"

    @pytest.mark.parametrize("path", ["/expired", "/unauthenticated"])
    def test_401_challenges_with_bearer(self, client: TestClient, path: str) -> None:
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_expired_and_invalid_tokens_have_different_codes(self, client: TestClient) -> None:
        assert client.get("/expired").json()["error"]["code"] == "TOKEN_EXPIRED"
        assert client.get("/unauthenticated").json()["error"]["code"] == "UNAUTHENTICATED"

    def test_unknown_route_uses_the_same_shape(self, client: TestClient) -> None:
        response = client.get("/nope")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    def test_wrong_method(self, client: TestClient) -> None:
        response = client.post("/ok")
        assert response.status_code == 405
        assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"

    def test_validation_errors_never_echo_the_submitted_value(self, client: TestClient) -> None:
        response = client.post("/body", json={"password": 12345678, "extra": "hunter2"})
        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "12345678" not in response.text
        assert "hunter2" not in response.text
        assert body["error"]["details"]["errors"][0]["loc"] == ["body", "password"]

    def test_unexpected_errors_are_opaque(self, client: TestClient) -> None:
        response = client.get("/boom", headers={"X-Request-ID": "req-500"})
        assert response.status_code == 500
        error = response.json()["error"]
        assert error["code"] == "INTERNAL_ERROR"
        assert error["request_id"] == "req-500"
        assert "hunter2" not in response.text
        assert "Traceback" not in response.text
        assert response.headers["X-Request-ID"] == "req-500"


def test_every_error_code_has_an_http_status() -> None:
    assert set(STATUS_BY_CODE) == set(ErrorCode)


def test_status_table_matches_the_spec() -> None:
    assert STATUS_BY_CODE[ErrorCode.EMAIL_TAKEN] == 409
    assert STATUS_BY_CODE[ErrorCode.TOKEN_EXPIRED] == 401
    assert STATUS_BY_CODE[ErrorCode.RATE_LIMITED] == 429
    assert STATUS_BY_CODE[ErrorCode.DISPATCH_FAILED] == 503
    assert CovenantError.code is ErrorCode.INTERNAL_ERROR
