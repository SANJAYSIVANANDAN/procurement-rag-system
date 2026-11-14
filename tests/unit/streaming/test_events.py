from uuid import UUID

import pytest
from pydantic import ValidationError

from covenantiq.streaming.events import OrderViolation, check_order, is_terminal, parse_event
from covenantiq.streaming.publisher import InMemoryPublisher

MID = UUID(int=1)
CID = UUID(int=2)


def event(kind: str, **fields: object) -> object:
    return parse_event({"event": kind, "message_id": MID, **fields})


def start() -> object:
    return event("message_start", conversation_id=CID, seq=2)


def token(i: int = 0) -> object:
    return event("token", text="a ", index=i)


def citation() -> object:
    return event(
        "citation",
        citation={
            "marker": 1,
            "document_id": UUID(int=9),
            "document_title": "MSA",
            "page_start": 1,
            "page_end": 1,
            "clause_ref": "6.2",
            "quote": "q",
        },
    )


def complete() -> object:
    return event("message_complete", status="completed", grounding_status="grounded", content="x")


def error() -> object:
    return event("error", code="X", message="m", retryable=True)


class TestParsing:
    def test_an_event_is_chosen_by_its_name(self) -> None:
        assert parse_event(
            {"event": "token", "message_id": MID, "text": "x", "index": 0}
        ).event == ("token")

    @pytest.mark.parametrize(
        "raw",
        [
            {"event": "nope", "message_id": MID},
            {"event": "token", "message_id": MID, "text": "x"},  # no index
            {"event": "token", "text": "x", "index": 0},  # no message id
            {"event": "token", "message_id": MID, "text": "x", "index": 0, "extra": 1},
        ],
    )
    def test_malformed_events_are_rejected(self, raw: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            parse_event(raw)

    def test_only_complete_and_error_are_terminal(self) -> None:
        assert is_terminal(complete()) and is_terminal(error())  # type: ignore[arg-type]
        assert not is_terminal(token())  # type: ignore[arg-type]
        assert not is_terminal(citation())  # type: ignore[arg-type]


class TestOrdering:
    @pytest.mark.parametrize(
        "stream",
        [
            [start, complete],
            [start, error],
            [start, token, token, complete],
            [start, token, citation, citation, complete],
            [start, citation, error],
        ],
    )
    def test_valid_streams(self, stream: list[object]) -> None:
        check_order([build() for build in stream])  # type: ignore[operator]

    @pytest.mark.parametrize(
        ("stream", "why"),
        [
            ([token, complete], "message_start"),
            ([start, token], "no terminal"),
            ([start, complete, token], "after the terminal"),
            ([start, complete, error], "after the terminal"),
            ([start, citation, token, complete], "after a citation"),
            ([start, start, complete], "twice"),
        ],
    )
    def test_invalid_streams(self, stream: list[object], why: str) -> None:
        with pytest.raises(OrderViolation, match=why):
            check_order([build() for build in stream])  # type: ignore[operator]


async def test_the_in_memory_publisher_keeps_events_per_message_in_order() -> None:
    publisher = InMemoryPublisher()
    other = UUID(int=7)
    await publisher.publish(MID, start())  # type: ignore[arg-type]
    await publisher.publish(other, start())  # type: ignore[arg-type]
    await publisher.publish(MID, complete())  # type: ignore[arg-type]
    assert publisher.kinds(MID) == ["message_start", "message_complete"]
    assert publisher.kinds(other) == ["message_start"]
