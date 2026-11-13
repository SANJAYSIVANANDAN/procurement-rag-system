from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from covenantiq.core.errors import TokenExpired, Unauthenticated, ValidationFailed
from covenantiq.core.ids import uuid7

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"  # noqa: S105
CLOCK_SKEW_LEEWAY_SECONDS = 30

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128

# Passwords that satisfy the length rule but are still among the most common. Compared lowercased.
COMMON_PASSWORDS = frozenset(
    {
        "passwordpassword",
        "password1234",
        "password12345",
        "password123456",
        "password123!",
        "p@ssw0rd1234",
        "123456789012",
        "1234567890123",
        "12345678901234",
        "qwertyuiopas",
        "qwertyuiop12",
        "qwerty123456",
        "1q2w3e4r5t6y",
        "abcdefghijkl",
        "iloveyou1234",
        "welcome12345",
        "welcome123456",
        "letmein123456",
        "administrator",
        "adminadminadmin",
        "changemechangeme",
        "changeme12345",
        "trustno1trustno1",
        "monkey123456",
        "dragon123456",
        "football1234",
        "baseball1234",
        "superman1234",
        "sunshine1234",
        "princess1234",
    }
)


def validate_password_policy(password: str, *, email: str | None = None) -> None:
    """Raises ValidationFailed (with a safe message, never the password) if the policy fails."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValidationFailed(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValidationFailed(f"Password must be at most {PASSWORD_MAX_LENGTH} characters.")
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or len(set(password)) == 1:
        raise ValidationFailed("Password is too common. Choose a less predictable one.")
    if email is not None and lowered == email.strip().lower():
        raise ValidationFailed("Password must not be the same as the email address.")


class PasswordHasher:
    """Argon2id via pwdlib. Defaults are the spec's: 64 MiB memory, time cost 3, 4 lanes.

    Hashing is CPU and memory heavy; async callers run it in a thread.
    """

    def __init__(
        self, *, time_cost: int = 3, memory_cost_kib: int = 65536, parallelism: int = 4
    ) -> None:
        self._hash = PasswordHash(
            (
                Argon2Hasher(
                    time_cost=time_cost, memory_cost=memory_cost_kib, parallelism=parallelism
                ),
            )
        )

    def hash(self, password: str) -> str:
        return self._hash.hash(password)

    def verify(self, password: str, password_hash: str) -> bool:
        return self._hash.verify(password, password_hash)

    def verify_and_update(self, password: str, password_hash: str) -> tuple[bool, str | None]:
        """Verify, and return a fresh hash if the stored one uses outdated parameters."""
        return self._hash.verify_and_update(password, password_hash)


@dataclass(frozen=True)
class TokenClaims:
    user_id: UUID
    token_id: str
    issued_at: datetime
    expires_at: datetime


def create_access_token(
    user_id: UUID,
    *,
    secret: str,
    ttl_minutes: int,
    now: datetime | None = None,
) -> tuple[str, TokenClaims]:
    issued_at = (now or datetime.now(UTC)).replace(microsecond=0)
    expires_at = issued_at + timedelta(minutes=ttl_minutes)
    token_id = str(uuid7())
    payload = {
        "sub": str(user_id),
        "iat": issued_at,
        "exp": expires_at,
        "jti": token_id,
        "typ": ACCESS_TOKEN_TYPE,
    }
    token = jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)
    return token, TokenClaims(user_id, token_id, issued_at, expires_at)


def decode_access_token(token: str, *, secret: str) -> TokenClaims:
    """Validate signature, expiry, required claims and token type.

    Expiry has its own error (the client should log in again); every other failure is the same
    opaque Unauthenticated so callers learn nothing about why a token was rejected.
    """
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            leeway=CLOCK_SKEW_LEEWAY_SECONDS,
            options={"require": ["sub", "iat", "exp", "jti", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpired("The access token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthenticated("Invalid authentication token.") from exc

    if payload["typ"] != ACCESS_TOKEN_TYPE:
        raise Unauthenticated("Invalid authentication token.")
    try:
        user_id = UUID(str(payload["sub"]))
    except ValueError as exc:
        raise Unauthenticated("Invalid authentication token.") from exc
    return TokenClaims(
        user_id=user_id,
        token_id=str(payload["jti"]),
        issued_at=datetime.fromtimestamp(payload["iat"], UTC),
        expires_at=datetime.fromtimestamp(payload["exp"], UTC),
    )
