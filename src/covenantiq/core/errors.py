from enum import StrEnum
from typing import Any, ClassVar


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"
    ENCRYPTED_PDF = "ENCRYPTED_PDF"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"  # noqa: S105
    NOT_FOUND = "NOT_FOUND"
    EMAIL_TAKEN = "EMAIL_TAKEN"
    CONFLICT = "CONFLICT"
    GENERATION_IN_PROGRESS = "GENERATION_IN_PROGRESS"
    DOCUMENT_NOT_READY = "DOCUMENT_NOT_READY"
    RATE_LIMITED = "RATE_LIMITED"
    INGESTION_BACKLOG = "INGESTION_BACKLOG"
    TOO_MANY_ACTIVE_JOBS = "TOO_MANY_ACTIVE_JOBS"
    DISPATCH_FAILED = "DISPATCH_FAILED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    MODEL_REFUSED = "MODEL_REFUSED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class CovenantError(Exception):
    """Base for all domain errors. The API layer maps `code` to an HTTP status."""

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "details": self.details}


class TransientError(CovenantError):
    """A background task failed for a reason that may pass: the task is retried with backoff."""

    code = ErrorCode.DEPENDENCY_UNAVAILABLE


class PermanentTaskError(CovenantError):
    """A background task cannot succeed however often it is retried. The job is marked failed
    with `job_code` and the message is dead-lettered."""

    code = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        job_code: str = "PROCESSING_FAILED",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.job_code = job_code


class ValidationFailed(CovenantError):
    code = ErrorCode.VALIDATION_ERROR


class UnsupportedFileType(CovenantError):
    code = ErrorCode.UNSUPPORTED_FILE_TYPE


class FileTooLarge(CovenantError):
    code = ErrorCode.FILE_TOO_LARGE


class TooManyPages(CovenantError):
    code = ErrorCode.TOO_MANY_PAGES


class EncryptedPdf(CovenantError):
    code = ErrorCode.ENCRYPTED_PDF


class Unauthenticated(CovenantError):
    code = ErrorCode.UNAUTHENTICATED


class InvalidCredentials(CovenantError):
    """Wrong email or password. Deliberately identical for both, to prevent user enumeration."""

    code = ErrorCode.INVALID_CREDENTIALS


class TokenExpired(CovenantError):
    code = ErrorCode.TOKEN_EXPIRED


class NotFound(CovenantError):
    """Also raised for resources the caller does not own, so existence is not revealed."""

    code = ErrorCode.NOT_FOUND


class EmailTaken(CovenantError):
    code = ErrorCode.EMAIL_TAKEN


class Conflict(CovenantError):
    code = ErrorCode.CONFLICT


class GenerationInProgress(CovenantError):
    code = ErrorCode.GENERATION_IN_PROGRESS


class DocumentNotReady(CovenantError):
    code = ErrorCode.DOCUMENT_NOT_READY


class RateLimited(CovenantError):
    code = ErrorCode.RATE_LIMITED

    def __init__(
        self,
        message: str,
        *,
        retry_after: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after = retry_after


class IngestionBacklog(CovenantError):
    code = ErrorCode.INGESTION_BACKLOG

    def __init__(
        self,
        message: str,
        *,
        retry_after: int = 30,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after = retry_after


class TooManyActiveJobs(CovenantError):
    code = ErrorCode.TOO_MANY_ACTIVE_JOBS


class DispatchFailed(CovenantError):
    code = ErrorCode.DISPATCH_FAILED


class DependencyUnavailable(CovenantError):
    code = ErrorCode.DEPENDENCY_UNAVAILABLE


class ModelRefused(CovenantError):
    code = ErrorCode.MODEL_REFUSED
