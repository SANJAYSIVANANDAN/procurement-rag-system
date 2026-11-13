from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection

# src/covenantiq/db/migrations.py -> repository (or /app in the image) root
_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def alembic_config() -> Config:
    return Config(str(_ALEMBIC_INI))


@lru_cache
def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("no Alembic revisions found")
    return head


async def current_revision(connection: AsyncConnection) -> str | None:
    """The revision the database is at, or None if it has never been migrated."""
    try:
        result = await connection.execute(text("SELECT version_num FROM alembic_version"))
    except ProgrammingError:
        return None
    return result.scalar_one_or_none()
