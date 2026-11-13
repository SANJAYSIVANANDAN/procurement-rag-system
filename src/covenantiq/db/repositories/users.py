from typing import ClassVar
from uuid import UUID

from sqlalchemy import select

from covenantiq.db.models import User
from covenantiq.db.repositories.base import Repository


class UserRepository(Repository):
    """Users are not owned data, so this is the one repository without an OwnershipScope."""

    user_data: ClassVar[bool] = False

    async def get(self, user_id: UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def add(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        return user
