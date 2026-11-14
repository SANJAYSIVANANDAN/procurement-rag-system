"""Celery lifecycle hooks, connected by importing this module (it is in the app's `include`)."""

from typing import Any

from celery.signals import setup_logging, worker_process_init, worker_process_shutdown, worker_ready

from covenantiq.core.config import get_settings
from covenantiq.core.enums import LogFormat
from covenantiq.core.logging import configure_logging, get_logger
from covenantiq.workers import queues, runtime

log = get_logger(__name__)


@setup_logging.connect
def configure_worker_logging(**_kwargs: Any) -> None:
    """Connecting to this signal stops Celery installing its own handlers, so worker logs share
    the JSON pipeline (and its redaction) with the API."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format or LogFormat.JSON)


@worker_process_init.connect
def init_worker_process(**_kwargs: Any) -> None:
    runtime.init_runtime()
    warm_up = getattr(runtime.container().extractor, "warm_up", None)
    if callable(warm_up):
        warm_up()  # load the Docling models now, not during the first job
    log.info("worker.process_ready")


@worker_process_shutdown.connect
def shutdown_worker_process(**_kwargs: Any) -> None:
    runtime.shutdown_runtime()


@worker_ready.connect
def declare_dead_letter_topology(sender: Any = None, **_kwargs: Any) -> None:
    """Make sure the dead-letter queues exist before the first failure needs them."""
    if sender is not None:
        queues.declare_topology(sender.app)
        log.info("worker.topology_declared")
