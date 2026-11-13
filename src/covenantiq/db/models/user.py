from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from covenantiq.core.ids import uuid7
from covenantiq.db.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    password_hash: Mapped[str]
    full_name: Mapped[str | None]
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
