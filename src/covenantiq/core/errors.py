from enum import StrEnum
from typing import Any, ClassVar


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"  # noqa: S105
    NOT_FOUND = "NOT_FOUND"
    EMAIL_TAKEN = "EMAIL_TAKEN"
    GENERATION_IN_PROGRESS = "GENERATION_IN_PROGRESS"
    DOCUMENT_NOT_READY = "DOCUMENT_NOT_READY"
    ASSESSMENT_REQUIRED = "ASSESSMENT_REQUIRED"
    INCOMPATIBLE_ASSESSMENTS = "INCOMPATIBLE_ASSESSMENTS"
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


class ValidationFailed(CovenantError):
    code = ErrorCode.VALIDATION_ERROR


class UnsupportedFileType(CovenantError):
    code = ErrorCode.UNSUPPORTED_FILE_TYPE


class FileTooLarge(CovenantError):
    code = ErrorCode.FILE_TOO_LARGE


class TooManyPages(CovenantError):
    code = ErrorCode.TOO_MANY_PAGES


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


class GenerationInProgress(CovenantError):
    code = ErrorCode.GENERATION_IN_PROGRESS


class DocumentNotReady(CovenantError):
    code = ErrorCode.DOCUMENT_NOT_READY


class AssessmentRequired(CovenantError):
    code = ErrorCode.ASSESSMENT_REQUIRED


class IncompatibleAssessments(CovenantError):
    code = ErrorCode.INCOMPATIBLE_ASSESSMENTS


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


class TooManyActiveJobs(CovenantError):
    code = ErrorCode.TOO_MANY_ACTIVE_JOBS


class DispatchFailed(CovenantError):
    code = ErrorCode.DISPATCH_FAILED


class DependencyUnavailable(CovenantError):
    code = ErrorCode.DEPENDENCY_UNAVAILABLE


class ModelRefused(CovenantError):
    code = ErrorCode.MODEL_REFUSED
