from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from covenantiq.core.enums import JobStatus, ProcessingStage
from covenantiq.core.ids import uuid7
from covenantiq.db.base import Base, OwnedMixin


class DocumentProcessingJob(Base, OwnedMixin):
    __tablename__ = "document_processing_jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[JobStatus] = mapped_column(default=JobStatus.QUEUED)
    stage: Mapped[ProcessingStage | None]
    progress: Mapped[float] = mapped_column(default=0.0, server_default=text("0"))
    attempt: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    celery_task_id: Mapped[str | None]
    error_code: Mapped[str | None]
    error_message: Mapped[str | None]
    heartbeat_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


# A document can have only one active ingestion job.
Index(
    "uq_document_processing_jobs_active_document",
    DocumentProcessingJob.document_id,
    unique=True,
    postgresql_where=text("status IN ('queued', 'running')"),
)
