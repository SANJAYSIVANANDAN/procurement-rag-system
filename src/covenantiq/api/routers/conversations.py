from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from covenantiq.api.deps import ContainerDep, CurrentPrincipal, rate_limit
from covenantiq.api.schemas.common import ErrorResponse
from covenantiq.api.schemas.conversations import (
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationPage,
    ConversationUpdate,
    MessageOut,
    MessagePage,
)
from covenantiq.api.schemas.documents import DocumentOut

router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(rate_limit("default"))],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate, principal: CurrentPrincipal, container: ContainerDep
) -> ConversationOut:
    created = await container.conversation_service.create(
        principal,
        title=payload.title,
        document_ids=payload.document_ids,
        vendor_id=payload.vendor_id,
    )
    return ConversationOut.model_validate(created)


@router.get("", response_model=ConversationPage)
async def list_conversations(
    principal: CurrentPrincipal,
    container: ContainerDep,
    archived: Annotated[bool, Query(description="Include archived conversations")] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
) -> ConversationPage:
    page = await container.conversation_service.list(
        principal, include_archived=archived, limit=limit, cursor=cursor
    )
    return ConversationPage(
        items=[ConversationOut.model_validate(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: UUID, principal: CurrentPrincipal, container: ContainerDep
) -> ConversationDetail:
    detail = await container.conversation_service.get(principal, conversation_id)
    return ConversationDetail(
        **ConversationOut.model_validate(detail.conversation).model_dump(),
        documents=[DocumentOut.model_validate(d) for d in detail.documents],
    )


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    conversation_id: UUID,
    payload: ConversationUpdate,
    principal: CurrentPrincipal,
    container: ContainerDep,
) -> ConversationOut:
    changes: dict[str, Any] = payload.model_dump(exclude_unset=True)
    updated = await container.conversation_service.update(principal, conversation_id, changes)
    return ConversationOut.model_validate(updated)


@router.get("/{conversation_id}/messages", response_model=MessagePage)
async def list_messages(
    conversation_id: UUID,
    principal: CurrentPrincipal,
    container: ContainerDep,
    before_seq: Annotated[
        int | None, Query(ge=1, description="Only messages older than this")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MessagePage:
    page = await container.conversation_service.messages(
        principal, conversation_id, before_seq=before_seq, limit=limit
    )
    return MessagePage(
        items=[MessageOut.model_validate(item) for item in page.items],
        next_cursor=page.next_cursor,
    )
