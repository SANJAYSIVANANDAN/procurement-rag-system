import re
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import REAL, DateTime, ForeignKey, MetaData, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from covenantiq.core.enums import DB_ENUMS

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def enum_type_name(enum_cls: type[StrEnum]) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", enum_cls.__name__).lower()


def pg_enum(enum_cls: type[StrEnum]) -> sa.Enum:
    return sa.Enum(
        enum_cls,
        name=enum_type_name(enum_cls),
        values_callable=lambda members: [member.value for member in members],
    )


class Base(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        list[str]: ARRAY(Text),
        str: Text,
        float: REAL,
        datetime: DateTime(timezone=True),
        **{enum_cls: pg_enum(enum_cls) for enum_cls in DB_ENUMS},
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class OwnedMixin:
    @declared_attr
    def owner_id(cls) -> Mapped[UUID]:
        return mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
