from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from covenantiq.api.schemas.documents import DocumentOut
from covenantiq.core.enums import GroundingStatus, MessageRole, MessageStatus


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    document_ids: list[UUID] = Field(default_factory=list, max_length=200)
    vendor_id: UUID | None = None


class ConversationUpdate(BaseModel):
    """Only the fields sent are changed; send `null` to clear the title or the vendor."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    document_ids: list[UUID] | None = Field(default=None, max_length=200)
    archived: bool | None = None
    vendor_id: UUID | None = None


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str | None
    vendor_id: UUID | None
    archived: bool
    document_ids: list[UUID]
    last_message_seq: int
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    documents: list[DocumentOut]


class ConversationPage(BaseModel):
    items: list[ConversationOut]
    next_cursor: str | None


class CitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    marker: int | None
    document_id: UUID
    document_title: str
    page_start: int | None
    page_end: int | None
    clause_ref: str | None
    quote: str
    validated: bool


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    seq: int
    role: MessageRole
    content: str
    status: MessageStatus
    grounding_status: GroundingStatus | None
    created_at: datetime
    completed_at: datetime | None
    citations: list[CitationOut]


class MessagePage(BaseModel):
    """The newest messages of the page, oldest first. Pass `next_cursor` as `before_seq` for the
    next, older page; it is `null` when there is nothing older."""

    items: list[MessageOut]
    next_cursor: str | None
