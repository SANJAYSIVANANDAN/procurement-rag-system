import pytest
from celery import Celery

from covenantiq.core.config import Settings


@pytest.fixture
def app(valid_env: dict[str, str]) -> Celery:
    # Imported here because the module builds its app at import time and needs a valid env.
    from covenantiq.workers.celery_app import create_celery_app

    return create_celery_app(Settings(_env_file=None))  # type: ignore[call-arg]


def test_uses_rabbitmq_broker_and_redis_backend(app: Celery, valid_env: dict[str, str]) -> None:
    assert app.conf.broker_url == valid_env["CIQ_RABBITMQ_URL"]
    assert app.conf.result_backend == valid_env["CIQ_CELERY_RESULT_BACKEND"]
    assert app.conf.broker_url.startswith("amqp://")


def test_reliability_settings(app: Celery) -> None:
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.result_expires == 86400
    assert app.conf.accept_content == ["json"]


def test_no_application_tasks_yet(app: Celery) -> None:
    assert [name for name in app.tasks if not name.startswith("celery.")] == []
