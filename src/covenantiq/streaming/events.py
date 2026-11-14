"""The server-sent events of a chat reply (spec 26.2) and the order they must come in.

    message_start -> (status | token)* -> citation* -> message_complete | error

Exactly one terminal event ends a stream. The graph nodes write these as plain dicts; the runner
adds `message_id` and validates each one against these models before anything is published.
"""

from collections.abc import Iterable
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: UUID


class MessageStart(_Event):
    event: Literal["message_start"] = "message_start"
    conversation_id: UUID
    seq: int


class Status(_Event):
    event: Literal["status"] = "status"
    stage: str
    detail: str | None = None


class Token(_Event):
    event: Literal["token"] = "token"
    text: str
    index: int


class CitationPayload(BaseModel):
    marker: int | None
    document_id: UUID
    document_title: str
    page_start: int | None
    page_end: int | None
    clause_ref: str | None
    quote: str


class CitationEvent(_Event):
    event: Literal["citation"] = "citation"
    citation: CitationPayload


class MessageComplete(_Event):
    event: Literal["message_complete"] = "message_complete"
    status: str
    grounding_status: str | None
    content: str
    usage: dict[str, int] = Field(default_factory=dict)
    timings_ms: dict[str, int] = Field(default_factory=dict)


class ErrorEvent(_Event):
    event: Literal["error"] = "error"
    code: str
    message: str  # safe to show: never a provider message, a prompt or a stack trace
    retryable: bool


SSEEvent = Annotated[
    MessageStart | Status | Token | CitationEvent | MessageComplete | ErrorEvent,
    Field(discriminator="event"),
]
_ADAPTER: TypeAdapter[SSEEvent] = TypeAdapter(SSEEvent)

TERMINAL = ("message_complete", "error")


def parse_event(data: dict[str, Any]) -> SSEEvent:
    return _ADAPTER.validate_python(data)


def is_terminal(event: SSEEvent) -> bool:
    return event.event in TERMINAL


class OrderViolation(AssertionError):
    pass


def check_order(events: Iterable[SSEEvent]) -> None:
    """Raise if a stream breaks the ordering guarantee. Used by tests and by the runner."""
    phase = "start"  # start -> body -> citations -> done
    for event in events:
        kind = event.event
        if phase == "done":
            raise OrderViolation(f"{kind} after the terminal event")
        if phase == "start":
            if kind != "message_start":
                raise OrderViolation(f"stream began with {kind}, not message_start")
            phase = "body"
        elif kind == "message_start":
            raise OrderViolation("message_start twice")
        elif kind in ("status", "token"):
            if phase != "body":
                raise OrderViolation(f"{kind} after a citation")
        elif kind == "citation":
            phase = "citations"
        else:  # a terminal event
            phase = "done"
    if phase != "done":
        raise OrderViolation("the stream has no terminal event")
