from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from covenantiq.api.schemas.documents import DocumentOut

URL_PATTERN = r"^https?://\S+$"


class VendorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    industry: str | None = Field(default=None, max_length=200)
    website: str | None = Field(default=None, max_length=500, pattern=URL_PATTERN)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VendorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    industry: str | None = Field(default=None, max_length=200)
    website: str | None = Field(default=None, max_length=500, pattern=URL_PATTERN)
    metadata: dict[str, Any] | None = None


class VendorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    industry: str | None
    website: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class VendorDetail(VendorOut):
    documents: list[DocumentOut]


class VendorPage(BaseModel):
    items: list[VendorOut]
    next_cursor: str | None
