from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

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
