import asyncio

from covenantiq.api.container import Container
from covenantiq.core.logging import get_logger
from covenantiq.workers.queues import declare_topology

log = get_logger(__name__)


async def declare_broker_topology(container: Container) -> None:
    """Best effort at startup: the API must still come up if the broker is not yet reachable."""
    try:
        await asyncio.to_thread(declare_topology, container.celery_app)
    except Exception:
        log.warning("broker.topology_unavailable")


async def sweep_loop(container: Container, interval_seconds: float) -> None:
    """Re-dispatch stuck jobs every `interval_seconds` (one replica at a time, via a Redis lock)."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await container.sweep_service.run_once()
        except Exception:
            log.error("sweep.failed", exc_info=True)
