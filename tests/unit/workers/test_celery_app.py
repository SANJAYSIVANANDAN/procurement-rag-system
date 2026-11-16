import pytest
from celery import Celery

from covenantiq.core import task_names
from covenantiq.core.config import Settings
from covenantiq.workers import queues


@pytest.fixture
def app(valid_env: dict[str, str]) -> Celery:
    from covenantiq.workers.app_factory import create_celery_app

    return create_celery_app(Settings(_env_file=None))  # type: ignore[call-arg]


def test_uses_rabbitmq_broker_and_redis_backend(app: Celery, valid_env: dict[str, str]) -> None:
    assert app.conf.broker_url == valid_env["CIQ_RABBITMQ_URL"]
    assert app.conf.result_backend == valid_env["CIQ_CELERY_RESULT_BACKEND"]
    assert app.conf.broker_url.startswith("amqp://")


def test_a_publisher_only_app_has_no_result_backend(valid_env: dict[str, str]) -> None:
    from covenantiq.workers.app_factory import create_celery_app

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    publisher = create_celery_app(settings, publisher_only=True)
    worker = create_celery_app(settings)
    assert type(publisher.backend).__name__ == "DisabledBackend"
    assert type(worker.backend).__name__ == "RedisBackend"
    assert publisher.conf.broker_url == worker.conf.broker_url  # same broker, same routing
    assert publisher.conf.task_routes == worker.conf.task_routes


def test_reliability_settings(app: Celery) -> None:
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.result_expires == 86400
    assert app.conf.accept_content == ["json"]
    assert app.conf.task_track_started is True


def test_routing_configuration(app: Celery) -> None:
    assert app.conf.task_default_exchange == "covenantiq"
    assert app.conf.task_default_exchange_type == "direct"
    assert app.conf.task_queues == queues.QUEUES
    assert app.conf.task_routes == queues.ROUTES
    # a typo in a queue name must fail loudly instead of silently creating a queue
    assert app.conf.task_create_missing_queues is False


def test_event_settings_and_logging(app: Celery) -> None:
    assert app.conf.worker_send_task_events is True
    assert app.conf.task_send_sent_event is True
    assert app.conf.worker_hijack_root_logger is False


def test_workers_import_the_task_modules(app: Celery) -> None:
    assert set(app.conf.include) == {
        "covenantiq.workers.signals",
        "covenantiq.workers.tasks.ingestion",
        "covenantiq.workers.tasks.analysis",
    }


def test_all_six_tasks_are_registered_with_their_limits(valid_env: dict[str, str]) -> None:
    from covenantiq.workers.celery_app import celery_app
    from covenantiq.workers.tasks import analysis, ingestion  # noqa: F401

    expected = {
        task_names.PROCESS_DOCUMENT: (900, 960, 3),
        task_names.EMBED_DOCUMENT: (1500, 1560, 5),
        task_names.PURGE_DOCUMENT: (300, 330, 5),
        task_names.RUN_CONTRACT_ANALYSIS: (1800, 1860, 3),
        task_names.RUN_VENDOR_ASSESSMENT: (2700, 2760, 3),
    }
    for name, (soft, hard, retries) in expected.items():
        task = celery_app.tasks[name]
        assert (task.soft_time_limit, task.time_limit, task.max_retries) == (soft, hard, retries), (
            name
        )
    assert celery_app.tasks[task_names.EMBED_DOCUMENT].rate_limit == "30/m"


def test_job_tasks_know_their_kind_and_purge_has_none(valid_env: dict[str, str]) -> None:
    from covenantiq.workers.celery_app import celery_app
    from covenantiq.workers.tasks import analysis, ingestion  # noqa: F401

    assert celery_app.tasks[task_names.PROCESS_DOCUMENT].job_kind == "document_processing"
    assert celery_app.tasks[task_names.RUN_CONTRACT_ANALYSIS].job_kind == "analysis"
    assert celery_app.tasks[task_names.PURGE_DOCUMENT].job_kind is None


def test_importing_the_factory_and_cli_needs_no_configuration() -> None:
    """`covenantiq --help` must work on a machine with no CIQ_* settings at all."""
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("CIQ_")}
    code = "import covenantiq.workers.app_factory, covenantiq.cli"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stderr
