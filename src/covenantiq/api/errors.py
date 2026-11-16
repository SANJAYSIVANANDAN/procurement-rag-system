from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from covenantiq.api.middleware import REQUEST_ID_HEADER
from covenantiq.core.errors import CovenantError, ErrorCode
from covenantiq.core.logging import get_logger

log = get_logger(__name__)

STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.UNSUPPORTED_FILE_TYPE: 400,
    ErrorCode.FILE_TOO_LARGE: 413,
    ErrorCode.TOO_MANY_PAGES: 400,
    ErrorCode.ENCRYPTED_PDF: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.INVALID_CREDENTIALS: 401,
    ErrorCode.TOKEN_EXPIRED: 401,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.EMAIL_TAKEN: 409,
    ErrorCode.CONFLICT: 409,
    ErrorCode.GENERATION_IN_PROGRESS: 409,
    ErrorCode.DOCUMENT_NOT_READY: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INGESTION_BACKLOG: 429,
    ErrorCode.TOO_MANY_ACTIVE_JOBS: 429,
    ErrorCode.DISPATCH_FAILED: 503,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.MODEL_REFUSED: 502,
    ErrorCode.INTERNAL_ERROR: 500,
}

# Starlette's own errors (unknown route, wrong method, ...) get the same body shape.
HTTP_ERROR_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 413: "FILE_TOO_LARGE"}


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return str(value) if value is not None else None


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | list[Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    response_headers = dict(headers or {})
    if request_id is not None:
        response_headers[REQUEST_ID_HEADER] = request_id
    if status_code == 401:
        response_headers.setdefault("WWW-Authenticate", "Bearer")
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": details if details is not None else {},
            "request_id": request_id,
        }
    }
    return JSONResponse(body, status_code=status_code, headers=response_headers)


async def handle_domain_error(request: Request, exc: Exception) -> JSONResponse:
    exc = cast(CovenantError, exc)
    retry_after = getattr(exc, "retry_after", None)
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return error_response(
        request,
        status_code=STATUS_BY_CODE[exc.code],
        code=exc.code.value,
        message=exc.message,
        details=exc.details,
        headers=headers,
    )


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, exc)
    # Only where and why: never the submitted value, which may be a password.
    problems = [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return error_response(
        request,
        status_code=422,
        code=ErrorCode.VALIDATION_ERROR.value,
        message="The request is invalid.",
        details={"errors": problems},
    )


async def handle_http_error(request: Request, exc: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, exc)
    code = HTTP_ERROR_CODES.get(exc.status_code, "HTTP_ERROR")
    return error_response(
        request,
        status_code=exc.status_code,
        code=code,
        message=str(exc.detail),
        headers=dict(exc.headers) if exc.headers else None,
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    log.error("http.unhandled_exception", exc_type=type(exc).__name__, exc_info=exc)
    return error_response(
        request,
        status_code=500,
        code=ErrorCode.INTERNAL_ERROR.value,
        message="An unexpected error occurred.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(CovenantError, handle_domain_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
