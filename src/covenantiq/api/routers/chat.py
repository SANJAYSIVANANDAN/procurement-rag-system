from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from sse_starlette.sse import EventSourceResponse

from covenantiq.api.deps import ContainerDep, CurrentPrincipal, rate_limit
from covenantiq.api.schemas.common import ErrorResponse
from covenantiq.api.schemas.conversations import MessageAccepted, MessageSubmit

router = APIRouter(
    prefix="/conversations",
    tags=["chat"],
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "A reply is already being written"},
        503: {"model": ErrorResponse, "description": "Chat is temporarily unavailable"},
    },
)


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("chat_submit"))],
    responses={200: {"model": MessageAccepted, "description": "Already submitted (idempotent)"}},
)
async def send_message(
    conversation_id: UUID,
    payload: MessageSubmit,
    response: Response,
    principal: CurrentPrincipal,
    container: ContainerDep,
) -> MessageAccepted:
    """Ask a question. The reply is written in the background; follow it at `stream_url`, or read
    the finished message from the conversation's messages. Sending the same `client_message_id`
    again returns the original ids with 200 and generates nothing."""
    submitted = await container.chat_service.submit(
        principal,
        conversation_id,
        content=payload.content,
        client_message_id=payload.client_message_id,
        document_ids=payload.document_ids,
    )
    if submitted.replay:
        response.status_code = status.HTTP_200_OK
    return MessageAccepted(
        user_message_id=submitted.user_message_id,
        assistant_message_id=submitted.assistant_message_id,
        stream_url=(
            f"/api/v1/conversations/{conversation_id}/stream"
            f"?message_id={submitted.assistant_message_id}"
        ),
        status=submitted.status,
    )


@router.get(
    "/{conversation_id}/stream",
    response_class=EventSourceResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Server-sent events"}},
)
async def stream_reply(
    conversation_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    container: ContainerDep,
    message_id: Annotated[UUID, Query()],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> EventSourceResponse:
    """The events of one reply: `message_start`, then `status` and `token` events, the `citation`
    events, and one `message_complete` or `error`. Each frame's `id` can be sent back as
    `Last-Event-ID` to continue without duplicates or gaps. A reply that is already finished (or
    whose stream has expired) is replayed from the database. Disconnecting does not stop it."""
    frames = await container.stream_service.open(
        principal,
        conversation_id,
        message_id,
        last_event_id=last_event_id,
        disconnected=request.is_disconnected,
    )

    async def events() -> AsyncIterator[dict[str, str]]:
        async for frame in frames:
            yield {"id": frame.id, "event": frame.event, "data": frame.data}

    return EventSourceResponse(
        events(), ping=15, headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
