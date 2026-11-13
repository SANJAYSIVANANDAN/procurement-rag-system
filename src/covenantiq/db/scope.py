from dataclasses import dataclass
from typing import Any, TypeVar, cast
from uuid import UUID

from sqlalchemy import Delete, Select, Update

from covenantiq.db.base import OwnedMixin

_Statement = TypeVar("_Statement", bound=Select[Any] | Update | Delete)


@dataclass(frozen=True)
class OwnershipScope:
    """Who a query acts for. Every user-data repository method takes one (spec 15.7).

    V3 adds an organization id here, so call sites do not change.
    """

    owner_id: UUID

    def apply(self, statement: _Statement, model: type[OwnedMixin]) -> _Statement:
        return cast(_Statement, statement.where(model.owner_id == self.owner_id))
