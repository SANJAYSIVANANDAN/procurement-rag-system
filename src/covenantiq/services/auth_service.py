import asyncio
import secrets
from collections.abc import Callable

from sqlalchemy.exc import IntegrityError

from covenantiq.core.errors import EmailTaken, InvalidCredentials, Unauthenticated
from covenantiq.core.security import (
    PasswordHasher,
    create_access_token,
    decode_access_token,
    validate_password_policy,
)
from covenantiq.db.models import User
from covenantiq.db.uow import UnitOfWork
from covenantiq.services.dto import Principal, TokenDTO, UserDTO


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _to_dto(user: User) -> UserDTO:
    return UserDTO(
        id=user.id, email=user.email, full_name=user.full_name, created_at=user.created_at
    )


class AuthService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        hasher: PasswordHasher,
        *,
        jwt_secret: str,
        jwt_ttl_minutes: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._hasher = hasher
        self._jwt_secret = jwt_secret
        self._jwt_ttl_minutes = jwt_ttl_minutes
        # Verified against when the email is unknown, so a miss costs the same as a wrong password.
        self._dummy_hash = hasher.hash(secrets.token_urlsafe(24))

    async def register(self, *, email: str, password: str, full_name: str | None = None) -> UserDTO:
        email = normalize_email(email)
        validate_password_policy(password, email=email)
        password_hash = await asyncio.to_thread(self._hasher.hash, password)

        async with self._uow_factory() as uow:
            if await uow.users.get_by_email(email) is not None:
                raise EmailTaken("An account with this email already exists.")
            try:
                user = await uow.users.add(
                    User(
                        email=email,
                        password_hash=password_hash,
                        full_name=(full_name or "").strip() or None,
                    )
                )
            except IntegrityError:  # lost a race with a concurrent registration
                raise EmailTaken("An account with this email already exists.") from None
            await uow.commit()
            return _to_dto(user)

    async def login(self, *, email: str, password: str) -> TokenDTO:
        email = normalize_email(email)
        async with self._uow_factory() as uow:
            user = await uow.users.get_by_email(email)
            usable = user is not None and user.is_active
            stored_hash = user.password_hash if user is not None and usable else self._dummy_hash
            valid, upgraded_hash = await asyncio.to_thread(
                self._hasher.verify_and_update, password, stored_hash
            )
            if not (valid and usable) or user is None:
                raise InvalidCredentials("Incorrect email or password.")
            if upgraded_hash is not None:
                user.password_hash = upgraded_hash
                await uow.commit()
            user_id = user.id

        token, _ = create_access_token(
            user_id, secret=self._jwt_secret, ttl_minutes=self._jwt_ttl_minutes
        )
        return TokenDTO(access_token=token, expires_in=self._jwt_ttl_minutes * 60)

    async def authenticate(self, token: str) -> Principal:
        """Turn a bearer token into a Principal, re-checking that the user is still active."""
        claims = decode_access_token(token, secret=self._jwt_secret)
        async with self._uow_factory() as uow:
            user = await uow.users.get(claims.user_id)
            if user is None or not user.is_active:
                raise Unauthenticated("Invalid authentication token.")
            return Principal(user_id=user.id, email=user.email)

    async def get_me(self, principal: Principal) -> UserDTO:
        async with self._uow_factory() as uow:
            user = await uow.users.get(principal.user_id)
            if user is None or not user.is_active:
                raise Unauthenticated("Invalid authentication token.")
            return _to_dto(user)
