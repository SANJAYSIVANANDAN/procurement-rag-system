import logging
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from covenantiq.core.enums import LogFormat

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "token",
        "authorization",
        "api_key",
        "content",
        "text",
        "prompt",
        "completion",
        "quote",
        "chunk_text",
    }
)
_SENSITIVE_SUFFIXES = ("_password", "_token", "_secret", "_api_key")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SENSITIVE_KEYS or lowered.endswith(_SENSITIVE_SUFFIXES)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(k, str) and _is_sensitive(k) else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_redact(item) for item in value)
    return value


def redact_sensitive(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Mask sensitive keys, recursively. Loggers should only pass ids, counts and timings."""
    return {k: REDACTED if _is_sensitive(k) else _redact(v) for k, v in event_dict.items()}


class _StdoutHandler(logging.Handler):
    """Writes to whatever sys.stdout is at emit time (survives stream swaps by pytest/Celery)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stdout.write(self.format(record) + "\n")
            sys.stdout.flush()
        except Exception:
            self.handleError(record)


def configure_logging(level: str = "INFO", fmt: LogFormat = LogFormat.CONSOLE) -> None:
    """Route both structlog and stdlib logging (uvicorn, celery, ...) through one pipeline."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        redact_sensitive,
    ]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    renderer: Processor
    if fmt is LogFormat.JSON:
        renderer = structlog.processors.JSONRenderer()
        final: list[Processor] = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            # no local variables in a traceback: they hold passwords, prompts and contract text
            structlog.processors.ExceptionRenderer(
                structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
            ),
            renderer,
        ]
    else:
        renderer = structlog.dev.ConsoleRenderer(
            colors=sys.stdout.isatty(),
            exception_formatter=structlog.dev.RichTracebackFormatter(show_locals=False),
        )
        final = [structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer]

    handler = _StdoutHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(foreign_pre_chain=shared, processors=final)
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_context(
    *,
    request_id: str | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    job_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """Attach correlation ids to every log line emitted in the current context."""
    fields = {
        "request_id": request_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "job_id": job_id,
        "task_id": task_id,
    }
    structlog.contextvars.bind_contextvars(**{k: v for k, v in fields.items() if v is not None})


def current_request_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("request_id")
    return str(value) if value is not None else None


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
