import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import UUID

from covenantiq.core.logging import get_logger
from covenantiq.services.job_service import JobKind, JobService

log = get_logger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 30.0


@asynccontextmanager
async def heartbeat(
    jobs: JobService,
    kind: JobKind,
    job_id: UUID,
    *,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncIterator[None]:
    """Refresh a running job's `heartbeat_at` while a long step runs, so a redelivered message
    can tell a slow worker from a dead one (takeover happens after 10 silent minutes)."""

    async def beat() -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await jobs.touch(kind, job_id)
            except Exception:
                log.warning("heartbeat.failed", job_id=str(job_id))

    beater = asyncio.create_task(beat())
    try:
        yield
    finally:
        beater.cancel()
        with suppress(asyncio.CancelledError):
            await beater
