from collections.abc import Mapping

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from covenantiq.api.errors import error_response
from covenantiq.core.errors import ErrorCode

# Multipart framing and the small form fields (title, vendor id, contract type) travel with the
# file, so the body may exceed the file limit by a few hundred bytes. The service enforces the
# exact file limit; this allowance only needs to be generous enough for the framing.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


class BodySizeLimitMiddleware:
    """Refuse oversized request bodies without reading them into memory.

    Starlette parses a whole multipart body before the endpoint runs, so a size check inside the
    endpoint comes too late. This middleware rejects at once when Content-Length already exceeds
    the limit, and for chunked requests it aborts the read the moment the running total does,
    which stops the transfer instead of spooling an arbitrarily large file to disk.
    """

    def __init__(self, app: ASGIApp, *, limits: Mapping[tuple[str, str], int]) -> None:
        self.app = app
        self._limits = dict(limits)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = (
            self._limits.get((scope["method"], scope["path"])) if scope["type"] == "http" else None
        )
        if limit is None:
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            response = error_response(
                Request(scope, receive),
                status_code=413,
                code=ErrorCode.FILE_TOO_LARGE.value,
                message="The upload is too large.",
                details={"max_bytes": limit},
            )
            await response(scope, receive, send)
            return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail="The upload is too large.")
            return message

        await self.app(scope, counting_receive, send)
