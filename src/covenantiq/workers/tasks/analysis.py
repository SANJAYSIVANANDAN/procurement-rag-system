from typing import Any
from uuid import UUID

from covenantiq.core import task_names
from covenantiq.core.errors import PermanentTaskError
from covenantiq.services.job_service import ANALYSIS
from covenantiq.services.vendor_assessment_service import (
    DEPENDENCY_WAIT_SECONDS,
    MAX_DEPENDENCY_WAITS,
)
from covenantiq.workers import runtime
from covenantiq.workers.base_task import CovenantTask
from covenantiq.workers.celery_app import celery_app


@celery_app.task(
    bind=True,
    base=CovenantTask,
    name=task_names.RUN_CONTRACT_ANALYSIS,
    job_kind=ANALYSIS,
    max_retries=3,
    retry_backoff_max=300,
    soft_time_limit=1800,
    time_limit=1860,
)
def run_contract_analysis(
    self: CovenantTask, job_id: str, *, request_id: str = ""
) -> dict[str, Any]:
    return runtime.run_async(
        runtime.container().contract_analysis_service.run(
            UUID(job_id), attempt=self.request.retries
        )
    )


@celery_app.task(
    bind=True,
    base=CovenantTask,
    name=task_names.RUN_VENDOR_ASSESSMENT,
    job_kind=ANALYSIS,
    max_retries=3,
    retry_backoff_max=300,
    soft_time_limit=2700,
    time_limit=2760,
)
def run_vendor_assessment(
    self: CovenantTask, job_id: str, *, request_id: str = ""
) -> dict[str, Any]:
    outcome = runtime.run_async(
        runtime.container().vendor_assessment_service.run(
            UUID(job_id), attempt=self.request.retries
        )
    )
    if outcome["status"] == "waiting":
        # A contract analysis this job depends on is still running. The service has put the job
        # back to `queued`; ask to be delivered again later. A bounded wait that holds no worker.
        if self.request.retries >= MAX_DEPENDENCY_WAITS:
            raise PermanentTaskError(
                "A contract analysis this assessment depends on did not finish in time.",
                job_code="DEPENDENCY_NOT_READY",
            )
        raise self.retry(countdown=DEPENDENCY_WAIT_SECONDS, max_retries=MAX_DEPENDENCY_WAITS)
    return outcome
