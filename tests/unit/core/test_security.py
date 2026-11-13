from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from covenantiq.core.errors import TokenExpired, Unauthenticated, ValidationFailed
from covenantiq.core.security import (
    COMMON_PASSWORDS,
    PasswordHasher,
    create_access_token,
    decode_access_token,
    validate_password_policy,
)

SECRET = "s" * 40

# Cheap Argon2 parameters keep the suite fast; the defaults are checked separately.
FAST = {"time_cost": 1, "memory_cost_kib": 1024, "parallelism": 1}


class TestPasswordPolicy:
    def test_accepts_a_long_passphrase(self) -> None:
        validate_password_policy("correct horse battery staple")

    @pytest.mark.parametrize("password", ["", "short", "x" * 11])
    def test_rejects_short_passwords(self, password: str) -> None:
        with pytest.raises(ValidationFailed, match="at least 12"):
            validate_password_policy(password)

    def test_rejects_overlong_passwords(self) -> None:
        with pytest.raises(ValidationFailed, match="at most 128"):
            validate_password_policy("x" * 129)
        validate_password_policy("ab" * 64)  # exactly 128 is fine

    def test_rejects_common_passwords_case_insensitively(self) -> None:
        with pytest.raises(ValidationFailed, match="too common"):
            validate_password_policy("PasswordPassword")

    def test_rejects_repeated_single_character(self) -> None:
        with pytest.raises(ValidationFailed, match="too common"):
            validate_password_policy("a" * 20)

    def test_rejects_the_email_address_itself(self) -> None:
        with pytest.raises(ValidationFailed, match="email"):
            validate_password_policy("Pat.Analyst@example.com", email="pat.analyst@example.com")

    def test_error_messages_never_contain_the_password(self) -> None:
        secret = "hunter2hunter2"
        with pytest.raises(ValidationFailed) as exc:
            validate_password_policy(secret + "!" * 200)
        assert secret not in str(exc.value)

    def test_bundled_list_entries_are_lowercase_and_long_enough(self) -> None:
        assert all(p == p.lower() and len(p) >= 12 for p in COMMON_PASSWORDS)


class TestPasswordHasher:
    def test_hash_verifies_and_is_argon2id_with_a_salt(self) -> None:
        hasher = PasswordHasher(**FAST)
        first = hasher.hash("correct horse battery staple")
        second = hasher.hash("correct horse battery staple")
        assert first.startswith("$argon2id$")
        assert first != second
        assert hasher.verify("correct horse battery staple", first)
        assert not hasher.verify("wrong horse battery staple", first)

    def test_default_parameters_match_the_spec(self) -> None:
        digest = PasswordHasher().hash("correct horse battery staple")
        assert "m=65536,t=3,p=4" in digest

    def test_verify_and_update_upgrades_outdated_hashes(self) -> None:
        old = PasswordHasher(**FAST).hash("correct horse battery staple")
        valid, upgraded = PasswordHasher(
            time_cost=2, memory_cost_kib=2048, parallelism=1
        ).verify_and_update("correct horse battery staple", old)
        assert valid
        assert upgraded is not None
        assert "m=2048,t=2,p=1" in upgraded

    def test_verify_and_update_keeps_current_hashes(self) -> None:
        hasher = PasswordHasher(**FAST)
        digest = hasher.hash("correct horse battery staple")
        assert hasher.verify_and_update("correct horse battery staple", digest) == (True, None)
        assert hasher.verify_and_update("nope nope nope nope", digest) == (False, None)


class TestAccessToken:
    def test_round_trip(self) -> None:
        user_id = uuid4()
        token, issued = create_access_token(user_id, secret=SECRET, ttl_minutes=60)
        claims = decode_access_token(token, secret=SECRET)
        assert claims.user_id == user_id
        assert claims.token_id == issued.token_id
        assert claims.expires_at - claims.issued_at == timedelta(minutes=60)

    def test_payload_has_the_spec_claims(self) -> None:
        token, _ = create_access_token(uuid4(), secret=SECRET, ttl_minutes=5)
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        assert set(payload) == {"sub", "iat", "exp", "jti", "typ"}
        assert payload["typ"] == "access"

    def test_each_token_gets_a_unique_jti(self) -> None:
        a, _ = create_access_token(uuid4(), secret=SECRET, ttl_minutes=5)
        b, _ = create_access_token(uuid4(), secret=SECRET, ttl_minutes=5)
        assert (
            decode_access_token(a, secret=SECRET).token_id
            != decode_access_token(b, secret=SECRET).token_id
        )

    def test_expired_token_has_its_own_error(self) -> None:
        long_ago = datetime.now(UTC) - timedelta(hours=3)
        token, _ = create_access_token(uuid4(), secret=SECRET, ttl_minutes=60, now=long_ago)
        with pytest.raises(TokenExpired):
            decode_access_token(token, secret=SECRET)

    def test_clock_skew_leeway_of_30_seconds(self) -> None:
        just_expired = datetime.now(UTC) - timedelta(minutes=5, seconds=10)
        token, _ = create_access_token(uuid4(), secret=SECRET, ttl_minutes=5, now=just_expired)
        decode_access_token(token, secret=SECRET)  # expired 10 s ago: inside the leeway

        way_expired = datetime.now(UTC) - timedelta(minutes=5, seconds=40)
        token, _ = create_access_token(uuid4(), secret=SECRET, ttl_minutes=5, now=way_expired)
        with pytest.raises(TokenExpired):
            decode_access_token(token, secret=SECRET)

    def test_tampered_payload_is_rejected(self) -> None:
        token, _ = create_access_token(uuid4(), secret=SECRET, ttl_minutes=5)
        header, payload, signature = token.split(".")
        forged = jwt.encode(
            {**jwt.decode(token, options={"verify_signature": False}), "sub": str(uuid4())},
            "attacker-secret-attacker-secret-1234",
            algorithm="HS256",
        )
        for bad in (f"{header}.{payload}.{signature[:-2]}xx", forged):
            with pytest.raises(Unauthenticated):
                decode_access_token(bad, secret=SECRET)

    def test_wrong_secret_is_rejected(self) -> None:
        token, _ = create_access_token(uuid4(), secret=SECRET, ttl_minutes=5)
        with pytest.raises(Unauthenticated):
            decode_access_token(token, secret="t" * 40)

    def test_wrong_token_type_is_rejected(self) -> None:
        now = datetime.now(UTC)
        token = jwt.encode(
            {
                "sub": str(uuid4()),
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "jti": "x",
                "typ": "refresh",
            },
            SECRET,
            algorithm="HS256",
        )
        with pytest.raises(Unauthenticated):
            decode_access_token(token, secret=SECRET)

    @pytest.mark.parametrize("missing", ["sub", "iat", "exp", "jti", "typ"])
    def test_missing_required_claims_are_rejected(self, missing: str) -> None:
        now = datetime.now(UTC)
        payload = {
            "sub": str(uuid4()),
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "jti": "x",
            "typ": "access",
        }
        del payload[missing]
        token = jwt.encode(payload, SECRET, algorithm="HS256")
        with pytest.raises(Unauthenticated):
            decode_access_token(token, secret=SECRET)

    def test_subject_must_be_a_uuid(self) -> None:
        now = datetime.now(UTC)
        token = jwt.encode(
            {
                "sub": "not-a-uuid",
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "jti": "x",
                "typ": "access",
            },
            SECRET,
            algorithm="HS256",
        )
        with pytest.raises(Unauthenticated):
            decode_access_token(token, secret=SECRET)

    def test_unsigned_tokens_are_rejected(self) -> None:
        now = datetime.now(UTC)
        unsigned = jwt.encode(
            {
                "sub": str(uuid4()),
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "jti": "x",
                "typ": "access",
            },
            key=None,
            algorithm="none",
        )
        with pytest.raises(Unauthenticated):
            decode_access_token(unsigned, secret=SECRET)

    @pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "Bearer xyz"])
    def test_garbage_is_rejected(self, garbage: str) -> None:
        with pytest.raises(Unauthenticated):
            decode_access_token(garbage, secret=SECRET)
