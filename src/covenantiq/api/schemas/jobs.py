from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from covenantiq.core.enums import JobStatus
from covenantiq.services.dto import JobDTO


class JobError(BaseModel):
    code: str
    message: str | None = None


class JobOut(BaseModel):
    id: UUID
    kind: Literal[
        "document_processing", "contract_analysis", "vendor_assessment", "vendor_comparison"
    ]
    status: JobStatus
    stage: str | None
    progress: float = Field(ge=0, le=1)
    attempt: int = 0
    error: JobError | None = None


def job_to_out(job: JobDTO) -> JobOut:
    return JobOut.model_validate(
        {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "stage": job.stage,
            "progress": job.progress,
            "attempt": job.attempt,
            "error": {"code": job.error_code, "message": job.error_message}
            if job.error_code
            else None,
        }
    )


def job_out(job: JobDTO | None) -> JobOut | None:
    return None if job is None else job_to_out(job)
