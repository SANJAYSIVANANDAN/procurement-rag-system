from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from covenantiq.db.scope import OwnershipScope


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. Services receive this and build an OwnershipScope from it."""

    user_id: UUID
    email: str
    roles: frozenset[str] = frozenset({"owner"})

    def scope(self) -> OwnershipScope:
        return OwnershipScope(self.user_id)


@dataclass(frozen=True)
class UserDTO:
    id: UUID
    email: str
    full_name: str | None
    created_at: datetime


@dataclass(frozen=True)
class TokenDTO:
    access_token: str
    expires_in: int
