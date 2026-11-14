from typing import Any

from covenantiq.core import task_names
from covenantiq.core.errors import PermanentTaskError
from covenantiq.services.job_service import ANALYSIS
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
    raise PermanentTaskError(
        "Contract analysis arrives in Milestone 12.", job_code="NOT_IMPLEMENTED"
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
    raise PermanentTaskError(
        "Vendor assessment arrives in Milestone 13.", job_code="NOT_IMPLEMENTED"
    )


@celery_app.task(
    bind=True,
    base=CovenantTask,
    name=task_names.RUN_VENDOR_COMPARISON,
    job_kind=ANALYSIS,
    max_retries=3,
    retry_backoff_max=300,
    soft_time_limit=600,
    time_limit=660,
)
def run_vendor_comparison(
    self: CovenantTask, job_id: str, *, request_id: str = ""
) -> dict[str, Any]:
    raise PermanentTaskError(
        "Vendor comparison arrives in Milestone 13.", job_code="NOT_IMPLEMENTED"
    )
