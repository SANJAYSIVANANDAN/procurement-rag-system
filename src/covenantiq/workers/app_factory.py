from celery import Celery

from covenantiq.core.config import Settings, get_settings
from covenantiq.workers import queues

TASK_MODULES = [
    "covenantiq.workers.signals",
    "covenantiq.workers.tasks.ingestion",
    "covenantiq.workers.tasks.analysis",
]


def create_celery_app(settings: Settings | None = None, *, publisher_only: bool = False) -> Celery:
    """Build the Celery app.

    `publisher_only` is for processes that only send tasks (the API and the CLI). They have no
    result backend: with one, every publish also connects to Redis to listen for the result,
    which stalls a request for minutes when Redis is down. Workers keep the backend and store
    results there (Redis DB 1, 24 h).
    """
    settings = settings or get_settings()
    app = Celery(
        "covenantiq",
        broker=settings.rabbitmq_url.get_secret_value(),
        backend=None if publisher_only else settings.celery_result_backend.get_secret_value(),
        include=TASK_MODULES,
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        result_expires=86400,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_track_started=True,
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=3,
        task_default_exchange=queues.EXCHANGE,
        task_default_exchange_type="direct",
        task_default_queue=queues.DOCUMENT_PROCESSING,
        task_create_missing_queues=False,
        task_queues=queues.QUEUES,
        task_routes=queues.ROUTES,
        worker_send_task_events=True,
        task_send_sent_event=True,
        worker_hijack_root_logger=False,
    )
    return app
