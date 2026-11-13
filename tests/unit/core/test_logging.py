import json
import logging
from collections.abc import Iterator

import pytest

from covenantiq.core.enums import LogFormat
from covenantiq.core.logging import (
    REDACTED,
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
)


@pytest.fixture
def json_logs(capsys: pytest.CaptureFixture[str]) -> Iterator[None]:
    configure_logging("DEBUG", LogFormat.JSON)
    yield
    clear_context()


def last_line(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])  # type: ignore[no-any-return]


@pytest.mark.usefixtures("json_logs")
def test_password_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    get_logger("test").info("login", password="x", user_id="u1")
    raw = capsys.readouterr().out
    record = json.loads(raw)
    assert record["password"] == REDACTED
    assert record["user_id"] == "u1"
    assert '"x"' not in raw


@pytest.mark.usefixtures("json_logs")
def test_redaction_is_recursive(capsys: pytest.CaptureFixture[str]) -> None:
    get_logger("test").info(
        "nested", payload={"items": [{"quote": "secret clause text", "page": 3}]}, api_key="k"
    )
    record = last_line(capsys)
    assert record["api_key"] == REDACTED
    assert record["payload"] == {"items": [{"quote": REDACTED, "page": 3}]}


@pytest.mark.usefixtures("json_logs")
def test_suffix_keys_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    get_logger("test").info("auth", access_token="abc", jwt_secret="def")
    record = last_line(capsys)
    assert record["access_token"] == REDACTED
    assert record["jwt_secret"] == REDACTED


@pytest.mark.usefixtures("json_logs")
def test_standard_fields_and_context(capsys: pytest.CaptureFixture[str]) -> None:
    bind_context(request_id="req-1", user_id="u-1")
    get_logger("covenantiq.test").warning("something", duration_ms=12)
    record = last_line(capsys)
    assert record["event"] == "something"
    assert record["level"] == "warning"
    assert record["logger"] == "covenantiq.test"
    assert record["request_id"] == "req-1"
    assert record["user_id"] == "u-1"
    assert record["duration_ms"] == 12
    assert "timestamp" in record


@pytest.mark.usefixtures("json_logs")
def test_clear_context_removes_bound_fields(capsys: pytest.CaptureFixture[str]) -> None:
    bind_context(request_id="req-1")
    clear_context()
    get_logger("test").info("after")
    assert "request_id" not in last_line(capsys)


@pytest.mark.usefixtures("json_logs")
def test_stdlib_loggers_share_the_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    logging.getLogger("uvicorn.error").info("started")
    record = last_line(capsys)
    assert record["event"] == "started"
    assert record["logger"] == "uvicorn.error"
