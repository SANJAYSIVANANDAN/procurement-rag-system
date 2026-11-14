import pytest

from covenantiq.core.errors import ValidationFailed
from covenantiq.services.pagination import decode_cursor, encode_cursor


def test_round_trip() -> None:
    cursor = encode_cursor("2026-11-13T10:00:00+00:00", "0193a1d2-0000-7000-8000-000000000001")
    assert decode_cursor(cursor, parts=2) == [
        "2026-11-13T10:00:00+00:00",
        "0193a1d2-0000-7000-8000-000000000001",
    ]


def test_cursor_is_url_safe_and_unpadded() -> None:
    cursor = encode_cursor("a" * 50, "b" * 7)
    assert "=" not in cursor
    assert all(c.isalnum() or c in "-_" for c in cursor)


@pytest.mark.parametrize(
    "cursor", ["", "not base64!!", "bm90IGpzb24", "W10", "W1wiYVwiXQ", "e30", "W1wiYVwiLDFd"]
)
def test_invalid_cursors_are_rejected(cursor: str) -> None:
    with pytest.raises(ValidationFailed, match="cursor"):
        decode_cursor(cursor, parts=2)


def test_wrong_number_of_parts_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        decode_cursor(encode_cursor("only-one"), parts=2)
