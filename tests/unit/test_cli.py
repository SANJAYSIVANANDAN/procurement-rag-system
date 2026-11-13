import pytest
from typer.testing import CliRunner

from covenantiq import __version__
from covenantiq.cli import app

runner = CliRunner()


def test_help_runs() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "check-config" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_migrate_upgrades_to_head(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("covenantiq.cli.command.upgrade", lambda _config, rev: calls.append(rev))
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0
    assert calls == ["head"]


def test_check_config_ok(valid_env: dict[str, str]) -> None:
    result = runner.invoke(app, ["check-config"])
    assert result.exit_code == 0
    assert "configuration ok" in result.output


def test_check_config_reports_problems_without_leaking_values(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIQ_JWT_SECRET", "short-secret-value")
    result = runner.invoke(app, ["check-config"])
    assert result.exit_code == 1
    assert "jwt_secret" in result.output
    assert "short-secret-value" not in result.output
