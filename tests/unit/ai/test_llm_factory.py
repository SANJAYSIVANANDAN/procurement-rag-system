from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from covenantiq.ai.llm import FakeLLMProvider, RetryingLLMProvider
from covenantiq.ai.llm.anthropic_provider import AnthropicProvider
from covenantiq.ai.llm.base import Route
from covenantiq.ai.llm.factory import build_llm
from covenantiq.ai.llm.routes import LLMRoute, RouteTable, load_routes
from covenantiq.core.config import Settings


def settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """A real (non-offline) configuration with every other provider switched off."""
    for name, value in {
        "CIQ_OFFLINE": "0",
        "CIQ_EMBEDDING_PROVIDER": "fake",
        "CIQ_RERANK_PROVIDER": "none",
        **env,
    }.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.usefixtures("valid_env")
class TestFactory:
    def test_the_fake_provider_for_offline_and_tests(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stack = build_llm(settings(monkeypatch, CIQ_LLM_PROVIDER="fake"))
        assert isinstance(stack.provider, FakeLLMProvider)

    def test_anthropic_is_wrapped_so_transient_failures_are_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack = build_llm(
            settings(monkeypatch, CIQ_LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-test-123")
        )
        assert isinstance(stack.provider, RetryingLLMProvider)
        assert stack.provider.model_name == "claude-opus-5-5"
        assert isinstance(stack.provider._inner, AnthropicProvider)

    def test_the_default_model_comes_from_settings_and_routes_can_override_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        routes = tmp_path / "routes.yaml"
        routes.write_text("routes:\n  classify:\n    model: claude-haiku-5-5\n    effort: low\n")
        stack = build_llm(
            settings(
                monkeypatch,
                CIQ_LLM_PROVIDER="fake",
                CIQ_LLM_MODEL_DEFAULT="claude-sonnet-5-5",
                CIQ_LLM_ROUTES_FILE=str(routes),
            )
        )
        assert stack.routes.model_for("classify") == "claude-haiku-5-5"
        assert stack.routes.model_for("answer") == "claude-sonnet-5-5"

    def test_anthropic_without_a_key_does_not_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
            settings(monkeypatch, CIQ_LLM_PROVIDER="anthropic")


class TestRoutesFile:
    def test_the_shipped_file_is_valid_and_every_route_has_sane_budgets(self) -> None:
        table = load_routes("claude-opus-5-5", Path("config/llm_routes.yaml"))
        for route in get_args(Route):
            config = table.get(route)
            assert 0 < config.max_tokens <= 128_000 and config.timeout_s > 0
        # the interactive hops are cheap and quick; the answer is not
        assert table.get("classify").effort == "low" and table.get("classify").timeout_s <= 5
        assert table.get("answer").max_tokens >= 1024

    def test_a_missing_file_means_defaults(self, tmp_path: Path) -> None:
        table = load_routes("m", tmp_path / "nope.yaml")
        assert table.get("answer") == LLMRoute() and table.model_for("answer") == "m"

    def test_a_misspelt_route_is_an_error_at_startup(self, tmp_path: Path) -> None:
        path = tmp_path / "r.yaml"
        path.write_text("routes:\n  clasify:\n    effort: low\n")
        with pytest.raises(ValueError, match="clasify"):
            load_routes("m", path)

    @pytest.mark.parametrize(
        "body",
        [
            "routes:\n  answer:\n    effort: turbo\n",
            "routes:\n  answer:\n    max_tokens: 0\n",
            "routes:\n  answer:\n    surprise: 1\n",
        ],
    )
    def test_malformed_values_are_errors(self, tmp_path: Path, body: str) -> None:
        path = tmp_path / "r.yaml"
        path.write_text(body)
        with pytest.raises(ValidationError):
            load_routes("m", path)

    def test_requests_carry_the_routes_budgets(self) -> None:
        table = RouteTable({"summarize": LLMRoute(max_tokens=300, timeout_s=7)}, "m")
        request = table.request("summarize", "sys", [])
        assert (request.max_tokens, request.timeout_s) == (300, 7)
