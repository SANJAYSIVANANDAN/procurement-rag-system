import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from covenantiq.api.container import Container
from covenantiq.core.config import get_settings
from covenantiq.core.security import PasswordHasher, create_access_token
from covenantiq.db.uow import UnitOfWork
from covenantiq.services.rate_limiter import RateLimit
from tests.fakes import FakeRedis
from tests.integration.api.conftest import FAST_HASHER, PASSWORD, Register, bearer, login

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"


def error_of(response: httpx.Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


class TestRegister:
    async def test_creates_a_user_and_never_returns_secrets(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            REGISTER,
            json={
                "email": "Pat.Analyst@Example.com",
                "password": PASSWORD,
                "full_name": "  Pat Analyst  ",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert set(body) == {"id", "email", "full_name", "created_at"}
        assert body["email"] == "pat.analyst@example.com"
        assert body["full_name"] == "Pat Analyst"
        assert PASSWORD not in response.text

    async def test_password_is_stored_as_an_argon2id_hash(
        self, client: httpx.AsyncClient, uow_factory: Callable[[], UnitOfWork]
    ) -> None:
        await client.post(REGISTER, json={"email": "a@example.com", "password": PASSWORD})
        async with uow_factory() as uow:
            user = await uow.users.get_by_email("a@example.com")
        assert user is not None
        assert user.password_hash.startswith("$argon2id$")
        assert PASSWORD not in user.password_hash

    async def test_blank_full_name_is_stored_as_null(self, register: Register) -> None:
        user = await register(full_name="   ")
        assert user["full_name"] is None

    async def test_duplicate_email_is_a_409_regardless_of_case(
        self, client: httpx.AsyncClient, register: Register
    ) -> None:
        await register(email="dup@example.com")
        response = await client.post(
            REGISTER, json={"email": "DUP@Example.COM", "password": PASSWORD}
        )
        assert response.status_code == 409
        assert error_of(response)["code"] == "EMAIL_TAKEN"

    async def test_concurrent_duplicate_registrations_create_one_user(
        self, client: httpx.AsyncClient
    ) -> None:
        payload = {"email": "race@example.com", "password": PASSWORD}
        responses = await asyncio.gather(*(client.post(REGISTER, json=payload) for _ in range(5)))
        assert sorted(r.status_code for r in responses) == [201, 409, 409, 409, 409]

    @pytest.mark.parametrize(
        "payload",
        [
            {"email": "not-an-email", "password": PASSWORD},
            {"email": "a@example.com", "password": "tooshort9"},
            {"email": "a@example.com", "password": "x" * 129},
            {"email": "a@example.com"},
            {"password": PASSWORD},
            {"email": "a@example.com", "password": PASSWORD, "is_admin": True},
        ],
    )
    async def test_invalid_requests_are_422(
        self, client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> None:
        response = await client.post(REGISTER, json=payload)
        assert response.status_code == 422
        assert error_of(response)["code"] == "VALIDATION_ERROR"
        assert "tooshort9" not in response.text  # the submitted value is never echoed

    async def test_common_passwords_are_refused(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            REGISTER, json={"email": "a@example.com", "password": "passwordpassword"}
        )
        assert response.status_code == 400
        assert error_of(response)["code"] == "VALIDATION_ERROR"
        assert "passwordpassword" not in response.text


class TestLoginAndMe:
    async def test_login_returns_a_bearer_token_that_opens_me(
        self, client: httpx.AsyncClient, register: Register
    ) -> None:
        user = await register()
        response = await client.post(LOGIN, json={"email": user["email"], "password": PASSWORD})
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == get_settings().jwt_ttl_minutes * 60

        me = await client.get(ME, headers=bearer(body["access_token"]))
        assert me.status_code == 200
        assert me.json()["id"] == user["id"]
        assert me.json()["email"] == user["email"]

    async def test_email_is_normalized_on_login(
        self, client: httpx.AsyncClient, register: Register
    ) -> None:
        user = await register(email="mixed@example.com")
        await login(client, "  MIXED@Example.com ", user["password"])

    async def test_oauth2_form_login_for_the_docs_authorize_button(
        self, client: httpx.AsyncClient, register: Register
    ) -> None:
        user = await register()
        response = await client.post(
            "/api/v1/auth/token", data={"username": user["email"], "password": PASSWORD}
        )
        assert response.status_code == 200
        assert response.json()["token_type"] == "bearer"

    async def test_wrong_password_and_unknown_email_are_indistinguishable(
        self, client: httpx.AsyncClient, register: Register
    ) -> None:
        user = await register()
        wrong = await client.post(
            LOGIN, json={"email": user["email"], "password": "nope-nope-nope"}
        )
        unknown = await client.post(
            LOGIN, json={"email": "ghost@example.com", "password": "nope-nope-nope"}
        )
        assert wrong.status_code == unknown.status_code == 401
        wrong_error, unknown_error = error_of(wrong), error_of(unknown)
        assert wrong_error["code"] == unknown_error["code"] == "INVALID_CREDENTIALS"
        assert wrong_error["message"] == unknown_error["message"]
        assert wrong_error["details"] == unknown_error["details"]

    async def test_unknown_email_still_pays_for_a_password_check(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        original = FAST_HASHER.verify_and_update

        def spy(password: str, password_hash: str) -> tuple[bool, str | None]:
            calls.append(password_hash)
            return original(password, password_hash)

        monkeypatch.setattr(FAST_HASHER, "verify_and_update", spy)
        await client.post(LOGIN, json={"email": "ghost@example.com", "password": "whatever-x"})
        assert len(calls) == 1
        assert calls[0].startswith("$argon2id$")

    async def test_inactive_users_cannot_log_in_or_use_tokens(
        self,
        client: httpx.AsyncClient,
        register: Register,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        user = await register()
        token = await login(client, user["email"])
        async with uow_factory() as uow:
            await uow.session.execute(text("UPDATE users SET is_active = false"))
            await uow.commit()

        response = await client.post(LOGIN, json={"email": user["email"], "password": PASSWORD})
        assert response.status_code == 401
        assert error_of(response)["code"] == "INVALID_CREDENTIALS"
        me = await client.get(ME, headers=bearer(token))
        assert me.status_code == 401
        assert error_of(me)["code"] == "UNAUTHENTICATED"

    async def test_outdated_hash_is_upgraded_on_login(
        self,
        client: httpx.AsyncClient,
        register: Register,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        user = await register()
        # a hash made with older, weaker parameters than the service's current ones
        weak = PasswordHasher(time_cost=1, memory_cost_kib=512, parallelism=1).hash(PASSWORD)
        async with uow_factory() as uow:
            await uow.session.execute(text("UPDATE users SET password_hash = :h"), {"h": weak})
            await uow.commit()

        await login(client, user["email"])
        async with uow_factory() as uow:
            stored = await uow.users.get_by_email(user["email"])
        assert stored is not None
        assert "m=1024,t=1,p=1" in stored.password_hash


class TestTokens:
    async def test_missing_token_is_401_unauthenticated(self, client: httpx.AsyncClient) -> None:
        response = await client.get(ME)
        assert response.status_code == 401
        assert error_of(response)["code"] == "UNAUTHENTICATED"
        assert response.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.parametrize("token", ["garbage", "a.b.c", "x" * 400])
    async def test_invalid_token_is_401_unauthenticated(
        self, client: httpx.AsyncClient, token: str
    ) -> None:
        response = await client.get(ME, headers=bearer(token))
        assert response.status_code == 401
        assert error_of(response)["code"] == "UNAUTHENTICATED"

    async def test_expired_token_is_401_token_expired(
        self, client: httpx.AsyncClient, register: Register
    ) -> None:
        user = await register()
        token, _ = create_access_token(
            UUID(user["id"]),
            secret=get_settings().jwt_secret.get_secret_value(),
            ttl_minutes=60,
            now=datetime.now(UTC) - timedelta(hours=2),
        )
        response = await client.get(ME, headers=bearer(token))
        assert response.status_code == 401
        assert error_of(response)["code"] == "TOKEN_EXPIRED"

    async def test_token_signed_with_another_secret_is_rejected(
        self, client: httpx.AsyncClient, register: Register
    ) -> None:
        user = await register()
        token, _ = create_access_token(UUID(user["id"]), secret="z" * 40, ttl_minutes=60)
        response = await client.get(ME, headers=bearer(token))
        assert response.status_code == 401
        assert error_of(response)["code"] == "UNAUTHENTICATED"

    async def test_token_for_a_deleted_user_is_rejected(
        self,
        client: httpx.AsyncClient,
        register: Register,
        uow_factory: Callable[[], UnitOfWork],
    ) -> None:
        user = await register()
        token = await login(client, user["email"])
        async with uow_factory() as uow:
            await uow.session.execute(text("DELETE FROM users"))
            await uow.commit()
        response = await client.get(ME, headers=bearer(token))
        assert response.status_code == 401
        assert error_of(response)["code"] == "UNAUTHENTICATED"

    async def test_token_for_an_unknown_user_id_is_rejected(
        self, client: httpx.AsyncClient
    ) -> None:
        token, _ = create_access_token(
            uuid4(), secret=get_settings().jwt_secret.get_secret_value(), ttl_minutes=60
        )
        response = await client.get(ME, headers=bearer(token))
        assert response.status_code == 401

    async def test_each_user_sees_only_themselves(
        self, client: httpx.AsyncClient, register: Register
    ) -> None:
        alice, bob = await register(), await register()
        alice_me = await client.get(ME, headers=bearer(await login(client, alice["email"])))
        bob_me = await client.get(ME, headers=bearer(await login(client, bob["email"])))
        assert alice_me.json()["id"] == alice["id"]
        assert bob_me.json()["id"] == bob["id"]


class TestRateLimiting:
    async def test_sixth_login_per_minute_for_the_same_email_is_429(
        self, client: httpx.AsyncClient, register: Register
    ) -> None:
        user = await register()
        for _ in range(5):
            response = await client.post(
                LOGIN, json={"email": user["email"], "password": "wrong-wrong-1"}
            )
            assert response.status_code == 401
        sixth = await client.post(LOGIN, json={"email": user["email"], "password": PASSWORD})
        assert sixth.status_code == 429
        assert error_of(sixth)["code"] == "RATE_LIMITED"
        assert int(sixth.headers["Retry-After"]) > 0

    async def test_successful_logins_count_too(
        self, client: httpx.AsyncClient, register: Register
    ) -> None:
        user = await register()
        for _ in range(5):
            await login(client, user["email"])
        response = await client.post(LOGIN, json={"email": user["email"], "password": PASSWORD})
        assert response.status_code == 429

    async def test_other_emails_are_unaffected_by_one_emails_limit(
        self, client: httpx.AsyncClient, register: Register
    ) -> None:
        first, second = await register(), await register()
        for _ in range(5):
            await client.post(LOGIN, json={"email": first["email"], "password": "wrong-wrong-1"})
        blocked = await client.post(LOGIN, json={"email": first["email"], "password": PASSWORD})
        assert blocked.status_code == 429
        await login(client, second["email"])

    async def test_eleventh_login_per_minute_from_one_ip_is_429(
        self, client: httpx.AsyncClient
    ) -> None:
        for n in range(10):
            response = await client.post(
                LOGIN, json={"email": f"user{n}@example.com", "password": "wrong-wrong-1"}
            )
            assert response.status_code == 401
        eleventh = await client.post(
            LOGIN, json={"email": "user99@example.com", "password": "wrong-wrong-1"}
        )
        assert eleventh.status_code == 429
        assert error_of(eleventh)["details"] == {"bucket": "auth_login_ip"}

    async def test_email_addresses_never_appear_in_redis_keys(
        self, client: httpx.AsyncClient, redis: FakeRedis
    ) -> None:
        await client.post(LOGIN, json={"email": "secret.person@example.com", "password": "x" * 12})
        keys = " ".join(redis.counters)
        assert "secret.person" not in keys
        assert "@" not in keys
        assert any(":auth_login_email:" in key for key in redis.counters)

    async def test_per_user_default_bucket_limits_authenticated_endpoints(
        self, client: httpx.AsyncClient, register: Register, container: Container
    ) -> None:
        user = await register()
        token = await login(client, user["email"])
        container.rate_limiter._limits["default"] = RateLimit(2, 60)
        assert (await client.get(ME, headers=bearer(token))).status_code == 200
        assert (await client.get(ME, headers=bearer(token))).status_code == 200
        third = await client.get(ME, headers=bearer(token))
        assert third.status_code == 429
        assert "Retry-After" in third.headers

    async def test_login_still_works_when_redis_is_down(
        self, client: httpx.AsyncClient, register: Register, redis: FakeRedis
    ) -> None:
        user = await register()
        redis.down = True
        await login(client, user["email"])


class TestRequestIdAndLogs:
    async def test_every_auth_response_carries_a_request_id(
        self, client: httpx.AsyncClient, register: Register
    ) -> None:
        user = await register()
        responses = [
            await client.post(LOGIN, json={"email": user["email"], "password": PASSWORD}),
            await client.post(LOGIN, json={"email": user["email"], "password": "bad-bad-bad-1"}),
            await client.get(ME),
            await client.get("/api/v1/does-not-exist"),
        ]
        for response in responses:
            assert response.headers["X-Request-ID"]
            if response.status_code >= 400:
                assert error_of(response)["request_id"] == response.headers["X-Request-ID"]

    async def test_supplied_request_id_is_echoed(self, client: httpx.AsyncClient) -> None:
        response = await client.get(ME, headers={"X-Request-ID": "trace-42"})
        assert response.headers["X-Request-ID"] == "trace-42"
        assert error_of(response)["request_id"] == "trace-42"

    async def test_passwords_and_tokens_never_appear_in_logs(
        self,
        client: httpx.AsyncClient,
        register: Register,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        user = await register(password="unmistakable-Register-Pw-91")
        token = await login(client, user["email"], "unmistakable-Register-Pw-91")
        await client.post(
            LOGIN, json={"email": user["email"], "password": "unmistakable-Wrong-Pw-17"}
        )
        await client.post(
            "/api/v1/auth/token",
            data={"username": user["email"], "password": "unmistakable-Form-Pw-33"},
        )
        await client.get(ME, headers=bearer(token))
        await client.post(REGISTER, json={"email": "x@example.com", "password": "short-pw-secret"})

        output = capsys.readouterr().out
        assert "http.request" in output  # logging is on, so the absence below means something
        for secret in (
            "unmistakable-Register-Pw-91",
            "unmistakable-Wrong-Pw-17",
            "unmistakable-Form-Pw-33",
            "short-pw-secret",
            token,
        ):
            assert secret not in output

    async def test_request_logs_carry_the_request_id(
        self, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        await client.get("/api/v1/health/live", headers={"X-Request-ID": "log-me-7"})
        records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
        completed = [r for r in records if r.get("event") == "http.request"]
        assert completed
        assert completed[-1]["request_id"] == "log-me-7"
        assert completed[-1]["status"] == 200


class TestHealthAndDocs:
    async def test_ready_reports_every_dependency(
        self, client: httpx.AsyncClient, broker_is_up: None
    ) -> None:
        response = await client.get("/api/v1/health/ready")
        assert response.status_code == 200
        assert response.json()["checks"] == {
            "database": "ok",
            "migrations": "ok",
            "redis": "ok",
            "rabbitmq": "ok",
        }

    async def test_not_ready_when_redis_is_down(
        self, client: httpx.AsyncClient, redis: FakeRedis, broker_is_up: None
    ) -> None:
        redis.down = True
        response = await client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["redis"] == "unavailable"

    async def test_health_endpoints_need_no_token(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/v1/health/live")).status_code == 200

    async def test_auth_endpoints_are_documented(self, client: httpx.AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()
        for path in ("register", "login", "token", "me"):
            assert f"/api/v1/auth/{path}" in schema["paths"]
        assert "OAuth2PasswordBearer" in schema["components"]["securitySchemes"]
