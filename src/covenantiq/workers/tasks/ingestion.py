from typing import Any
from uuid import UUID

from covenantiq.core import task_names
from covenantiq.services.job_service import PROCESSING
from covenantiq.workers import runtime
from covenantiq.workers.base_task import CovenantTask
from covenantiq.workers.celery_app import celery_app


@celery_app.task(
    bind=True,
    base=CovenantTask,
    name=task_names.PROCESS_DOCUMENT,
    job_kind=PROCESSING,
    max_retries=3,
    retry_backoff_max=300,
    soft_time_limit=900,
    time_limit=960,
)
def process_document(self: CovenantTask, job_id: str, *, request_id: str = "") -> dict[str, Any]:
    return runtime.run_async(
        runtime.container().ingestion_service.process(UUID(job_id), attempt=self.request.retries)
    )


@celery_app.task(
    bind=True,
    base=CovenantTask,
    name=task_names.EMBED_DOCUMENT,
    job_kind=PROCESSING,
    max_retries=5,
    retry_backoff_max=600,
    rate_limit="30/m",
    soft_time_limit=1500,
    time_limit=1560,
)
def embed_document(self: CovenantTask, job_id: str, *, request_id: str = "") -> dict[str, Any]:
    # The job's stage decides what is left to do, so a message that arrives under the other
    # task name still does the right thing.
    return runtime.run_async(
        runtime.container().ingestion_service.embed(UUID(job_id), attempt=self.request.retries)
    )


@celery_app.task(
    bind=True,
    base=CovenantTask,
    name=task_names.PURGE_DOCUMENT,
    max_retries=5,
    retry_backoff_max=300,
    soft_time_limit=300,
    time_limit=330,
)
def purge_document(self: CovenantTask, document_id: str, *, request_id: str = "") -> dict[str, Any]:
    return runtime.run_async(runtime.container().ingestion_service.purge(UUID(document_id)))
