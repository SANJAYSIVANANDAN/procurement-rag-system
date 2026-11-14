import random
from typing import Any, ClassVar, NoReturn
from uuid import UUID

from celery import Task
from celery.exceptions import Ignore, Reject, Retry, SoftTimeLimitExceeded
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from covenantiq.core.errors import PermanentTaskError, TransientError
from covenantiq.core.logging import bind_context, clear_context, get_logger
from covenantiq.services.job_service import JobKind
from covenantiq.workers import runtime

log = get_logger(__name__)

SOFT_TIMEOUT_RETRIES = 1


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, TransientError | ConnectionError | TimeoutError):
        return True
    if isinstance(exc, OperationalError | InterfaceError):
        return True
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


class CovenantTask(Task):
    """Base class for every task: log correlation, bounded retries, and a uniform ending.

    A task that cannot succeed (permanent error, retries exhausted, repeated timeout) ends in
    two steps, in this order: the job row is marked `failed` in PostgreSQL, then `Reject` sends
    the message to the dead-letter queue. PostgreSQL is therefore always the user-visible truth,
    whatever the broker does afterwards.
    """

    job_kind: ClassVar[JobKind | None] = None
    max_retries = 3
    retry_backoff_base = 5.0
    retry_backoff_max: float = 300.0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        job_id = args[0] if args and self.job_kind is not None else None
        bind_context(
            task_id=getattr(self.request, "id", None),
            job_id=str(job_id) if job_id else None,
            request_id=kwargs.get("request_id") or None,
        )
        try:
            return self._run_guarded(*args, **kwargs)
        finally:
            clear_context()

    def _run_guarded(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().__call__(*args, **kwargs)
        except (Retry, Ignore, Reject):
            raise
        except SoftTimeLimitExceeded as exc:
            if self.request.retries < SOFT_TIMEOUT_RETRIES:
                self._retry(exc, args)
            self._fail(args, exc, "TIMEOUT", "Processing took too long.")
        except PermanentTaskError as exc:
            self._fail(args, exc, exc.job_code, exc.message)
        except Exception as exc:
            if _is_transient(exc):
                if self.request.retries < self.max_retries:
                    self._retry(exc, args)
                self._fail(
                    args,
                    exc,
                    "TRANSIENT_FAILURE",
                    "A dependency stayed unavailable after several attempts.",
                )
            self._fail(args, exc, "INTERNAL_ERROR", "An unexpected error occurred.")

    def countdown(self, retries: int) -> float:
        """Exponential backoff with jitter, capped."""
        base = min(float(self.retry_backoff_max), self.retry_backoff_base * (2**retries))
        return float(base * (0.5 + random.random() / 2))  # noqa: S311  (jitter, not security)

    def _retry(self, exc: BaseException, args: tuple[Any, ...] = ()) -> NoReturn:
        log.warning("task.retry", error=type(exc).__name__, attempt=self.request.retries + 1)
        self._release(args)
        raise self.retry(exc=exc, countdown=self.countdown(self.request.retries))

    def _release(self, args: tuple[Any, ...]) -> None:
        """Put the job back to `queued` so the retried delivery can claim it."""
        if self.job_kind is None or not args:
            return
        try:
            runtime.run_async(
                runtime.container().job_service.release(self.job_kind, UUID(str(args[0])))
            )
        except Exception:
            # The retry still goes ahead; a stale heartbeat lets it take the job over in time.
            log.error("task.release_error", exc_info=True)

    def _fail(self, args: tuple[Any, ...], exc: BaseException, code: str, message: str) -> NoReturn:
        log.error("task.failed", code=code, error=type(exc).__name__)
        if self.job_kind is not None and args:
            try:
                runtime.run_async(
                    runtime.container().job_service.mark_failed(
                        self.job_kind, UUID(str(args[0])), code=code, message=message
                    )
                )
            except Exception:
                # The sweep and the stale-heartbeat takeover recover a job left unmarked.
                log.error("task.mark_failed_error", exc_info=True)
        raise Reject(message, requeue=False) from exc
