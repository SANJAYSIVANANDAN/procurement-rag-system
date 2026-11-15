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


SECRET_LOCAL = "hunter2-contract-text-ZEBRA-QUOKKA"


def _fails_holding_a_secret() -> None:
    document_text = SECRET_LOCAL  # noqa: F841 - a local a traceback could print
    raise RuntimeError("boom")


@pytest.mark.parametrize("fmt", [LogFormat.JSON, LogFormat.CONSOLE])
def test_a_logged_traceback_does_not_print_local_variables(
    fmt: LogFormat, capsys: pytest.CaptureFixture[str]
) -> None:
    """Locals hold passwords, prompts and contract text; a traceback must show code, not values."""
    configure_logging("DEBUG", fmt)
    try:
        _fails_holding_a_secret()
    except RuntimeError:
        get_logger("test").error("something.failed", exc_info=True)
    seen = capsys.readouterr()
    output = seen.out + seen.err
    assert "boom" in output  # the error and its stack are still there
    assert "_fails_holding_a_secret" in output
    assert SECRET_LOCAL not in output
    clear_context()
