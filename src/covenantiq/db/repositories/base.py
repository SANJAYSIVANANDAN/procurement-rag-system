from collections.abc import Collection
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from covenantiq.db.base import OwnedMixin
from covenantiq.db.scope import OwnershipScope


class Repository:
    """Base for all repositories.

    Repositories never commit (the unit of work does); they may flush to obtain generated
    values. Every public method on a user-data repository takes an OwnershipScope, unless its
    name starts with `system_` (used by workers acting on a job they have just loaded).
    """

    user_data: ClassVar[bool] = True

    def __init__(self, session: AsyncSession) -> None:
        self.session = session


def ensure_owned(scope: OwnershipScope, entity: OwnedMixin) -> None:
    if entity.owner_id != scope.owner_id:
        raise ValueError("entity owner does not match the ownership scope")


def like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def cas_transition(
    session: AsyncSession,
    model: Any,
    row_id: UUID,
    status_column: str,
    expected: Collection[Any],
    new: Any,
    **fields: Any,
) -> bool:
    """Compare-and-set: move `status_column` to `new` only if it currently holds one of
    `expected`. With concurrent callers exactly one wins; the rest get False."""
    column = getattr(model, status_column)
    statement = (
        update(model)
        .where(model.id == row_id, column.in_(list(expected)))
        .values({status_column: new, **fields})
        .returning(model.id)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(statement)
    return result.scalar_one_or_none() is not None
