from typing import Any

from pydantic import BaseModel


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | list[Any] = {}
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """The body of every 4xx/5xx response (spec 16.1)."""

    error: ErrorInfo
