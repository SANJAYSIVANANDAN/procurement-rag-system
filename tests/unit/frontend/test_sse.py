"""Following a reply's stream: parsing, reconnecting with the last id, and folding events."""

import json

import httpx
import pytest
import respx

from frontend.api_client import ApiClient, ApiError
from frontend.sse import ReplyState, StreamEvent, stream_events

BASE = "http://api.test/api/v1"
CID = "c1"
MID = "m1"
URL = f"{BASE}/conversations/{CID}/stream"


def frame(event: str, entry_id: str, **fields: object) -> bytes:
    data = json.dumps({"event": event, "message_id": MID, **fields})
    return f"id: {entry_id}\nevent: {event}\ndata: {data}\n\n".encode()


def sse(*frames: bytes) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=b"".join(frames)
    )


START = frame("message_start", "1-0", conversation_id=CID, seq=2)
TOKEN_A = frame("token", "2-0", text="The cap ", index=0)
TOKEN_B = frame("token", "3-0", text="is fees.", index=1)
CITATION = frame("citation", "4-0", citation={"marker": 1, "document_title": "MSA", "quote": "q"})
COMPLETE = frame(
    "message_complete",
    "5-0",
    status="completed",
    grounding_status="grounded",
    content="The cap is fees. [C1]",
)


@pytest.fixture
def api() -> ApiClient:
    return ApiClient(BASE, token="tok")


def kinds(events: list[StreamEvent]) -> list[str]:
    return [e.kind for e in events]


class TestStreamEvents:
    @respx.mock
    def test_events_come_in_order_and_stop_at_the_terminal_event(self, api: ApiClient) -> None:
        respx.get(URL).mock(return_value=sse(START, TOKEN_A, TOKEN_B, CITATION, COMPLETE))
        events = list(stream_events(api, CID, MID))
        assert kinds(events) == ["message_start", "token", "token", "citation", "message_complete"]
        assert events[1].data["text"] == "The cap "

    @respx.mock
    def test_a_dropped_connection_resumes_after_the_last_event_without_repeats(
        self, api: ApiClient
    ) -> None:
        route = respx.get(URL).mock(
            side_effect=[sse(START, TOKEN_A), sse(TOKEN_B, CITATION, COMPLETE)]
        )
        slept: list[float] = []

        events = list(stream_events(api, CID, MID, sleep=slept.append))

        assert kinds(events) == ["message_start", "token", "token", "citation", "message_complete"]
        assert "last-event-id" not in route.calls[0].request.headers
        assert route.calls[1].request.headers["last-event-id"] == "2-0"
        assert slept == [0.5]

    @respx.mock
    def test_a_connection_error_is_retried_too(self, api: ApiClient) -> None:
        respx.get(URL).mock(side_effect=[httpx.ReadError("reset"), sse(START, COMPLETE)])
        events = list(stream_events(api, CID, MID, sleep=lambda _s: None))
        assert kinds(events) == ["message_start", "message_complete"]

    @respx.mock
    def test_after_three_reconnects_it_ends_with_a_retryable_error(self, api: ApiClient) -> None:
        route = respx.get(URL).mock(return_value=sse(START))
        events = list(stream_events(api, CID, MID, sleep=lambda _s: None))
        assert route.call_count == 4  # the first try and three reconnects
        assert events[-1].kind == "error"
        assert events[-1].data["code"] == "STREAM_LOST" and events[-1].data["retryable"] is True

    @respx.mock
    def test_an_error_the_api_reports_is_raised_not_retried(self, api: ApiClient) -> None:
        body = {"error": {"code": "NOT_FOUND", "message": "Message not found.", "details": {}}}
        route = respx.get(URL).respond(status_code=404, json=body)
        with pytest.raises(ApiError) as caught:
            list(stream_events(api, CID, MID, sleep=lambda _s: None))
        assert caught.value.status == 404 and route.call_count == 1

    @respx.mock
    def test_an_error_event_ends_the_stream(self, api: ApiClient) -> None:
        err = frame("error", "2-0", code="STREAM_STALLED", message="stalled", retryable=True)
        respx.get(URL).mock(return_value=sse(START, err, TOKEN_A))
        events = list(stream_events(api, CID, MID))
        assert kinds(events) == ["message_start", "error"]

    @respx.mock
    def test_comment_pings_are_ignored(self, api: ApiClient) -> None:
        respx.get(URL).mock(return_value=sse(b": ping\n\n", START, b": ping\n\n", COMPLETE))
        assert kinds(list(stream_events(api, CID, MID))) == ["message_start", "message_complete"]


def ev(kind: str, **fields: object) -> StreamEvent:
    return StreamEvent(kind, {"event": kind, **fields})


class TestReplyState:
    def test_tokens_build_a_draft_and_the_final_text_replaces_it(self) -> None:
        state = ReplyState()
        assert state.apply(ev("message_start")) == ""
        assert state.apply(ev("status", stage="retrieving")) == ""
        assert state.apply(ev("token", text="The cap ")) == "The cap "
        state.apply(ev("token", text="is fees."))
        assert state.text == "The cap is fees." and not state.done

        state.apply(ev("citation", citation={"marker": 1}))
        state.apply(
            ev(
                "message_complete",
                content="The cap is fees. [C1]",
                status="completed",
                grounding_status="grounded",
                usage={"output_tokens": 9},
            )
        )

        assert state.done and state.text == "The cap is fees. [C1]"
        assert state.draft == "The cap is fees."  # the draft is kept, but text prefers the final
        assert (state.grounding, state.citations, state.usage) == (
            "grounded",
            [{"marker": 1}],
            {"output_tokens": 9},
        )

    def test_a_new_message_start_forgets_everything_shown_so_far(self) -> None:
        state = ReplyState()
        state.apply(ev("message_start"))
        state.apply(ev("status", stage="generating"))
        state.apply(ev("token", text="half an answ"))
        state.apply(ev("citation", citation={"marker": 1}))

        state.apply(ev("message_start"))  # a replay starts again

        assert (state.draft, state.stages, state.citations) == ("", [], [])

    def test_repeated_stages_are_listed_once_in_a_row(self) -> None:
        state = ReplyState()
        for stage in ["retrieving", "retrieving", "generating", "retrieving"]:
            state.apply(ev("status", stage=stage))
        assert state.stages == ["retrieving", "generating", "retrieving"]

    def test_an_error_ends_it_and_keeps_the_partial_text(self) -> None:
        state = ReplyState()
        state.apply(ev("token", text="partial"))
        state.apply(ev("error", code="INTERRUPTED", message="restarted", retryable=True))
        assert state.done and state.error is not None and state.error["code"] == "INTERRUPTED"
        assert state.text == "partial"
