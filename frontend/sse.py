"""Following a reply's server-sent events and turning them into what the chat page shows.

`stream_events` reads the stream and, when the connection drops, reconnects up to three times with
`Last-Event-ID`, so no event is repeated or missed. `ReplyState` folds the events into the pieces
the page renders. Neither imports Streamlit.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from frontend.api_client import ApiClient, ApiError

TERMINAL = ("message_complete", "error")
MAX_RECONNECTS = 3


@dataclass(frozen=True)
class StreamEvent:
    kind: str
    data: dict[str, Any]
    id: str | None = None


def _lost(message_id: str) -> StreamEvent:
    return StreamEvent(
        "error",
        {
            "event": "error",
            "message_id": message_id,
            "code": "STREAM_LOST",
            "message": "The connection was lost before the answer finished.",
            "retryable": True,
        },
    )


def stream_events(
    client: ApiClient,
    conversation_id: str,
    message_id: str,
    *,
    max_reconnects: int = MAX_RECONNECTS,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[StreamEvent]:
    """Yield the events of one reply until its terminal event.

    A stream that ends without a terminal event, or a connection that fails, is retried with the
    last event id (waiting a little longer each time). After `max_reconnects` retries a synthetic
    retryable `error` event (`STREAM_LOST`) ends it. An error the API itself reports (a 404, a 401)
    is raised, since reconnecting cannot help.
    """
    last_id: str | None = None
    attempt = 0
    while True:
        try:
            with client.open_stream(conversation_id, message_id, last_id) as source:
                for sse in source.iter_sse():
                    if not sse.data:
                        continue
                    data = json.loads(sse.data)
                    last_id = sse.id or last_id
                    yield StreamEvent(sse.event, data, sse.id)
                    if sse.event in TERMINAL:
                        return
        except (httpx.TransportError, ApiError) as error:
            if isinstance(error, ApiError) and error.code != "API_UNREACHABLE":
                raise
        attempt += 1
        if attempt > max_reconnects:
            yield _lost(message_id)
            return
        sleep(0.5 * attempt)


@dataclass
class ReplyState:
    """Everything the page needs to draw a reply from its events, in the order they arrive."""

    draft: str = ""
    stages: list[str] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    final: str | None = None
    status: str | None = None
    grounding: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    error: dict[str, Any] | None = None

    @property
    def done(self) -> bool:
        return self.final is not None or self.error is not None

    def apply(self, event: StreamEvent) -> str:
        """Fold one event in; return the text to append to the draft (empty for most events)."""
        data = event.data
        if event.kind == "message_start":
            # a replay or a restart begins again: forget what was shown
            self.draft, self.stages, self.citations = "", [], []
            self.final = self.status = self.grounding = None
            self.error = None
        elif event.kind == "status":
            stage = str(data.get("stage", ""))
            if stage and (not self.stages or self.stages[-1] != stage):
                self.stages.append(stage)
        elif event.kind == "token":
            text = str(data.get("text", ""))
            self.draft += text
            return text
        elif event.kind == "citation":
            self.citations.append(data["citation"])
        elif event.kind == "message_complete":
            self.final = str(data.get("content", ""))
            self.status = data.get("status")
            self.grounding = data.get("grounding_status")
            self.usage = data.get("usage") or {}
        elif event.kind == "error":
            self.error = data
        return ""

    @property
    def text(self) -> str:
        """The validated final text once there is one, otherwise the draft so far."""
        return self.final if self.final is not None else self.draft
