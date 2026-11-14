from dataclasses import dataclass

from covenantiq.ai.llm.anthropic_provider import AnthropicProvider
from covenantiq.ai.llm.base import LLMProvider
from covenantiq.ai.llm.fake import FakeLLMProvider
from covenantiq.ai.llm.retry import RetryingLLMProvider
from covenantiq.ai.llm.routes import RouteTable, load_routes
from covenantiq.core.config import Settings


@dataclass(frozen=True)
class LLMStack:
    provider: LLMProvider
    routes: RouteTable


def build_llm(settings: Settings) -> LLMStack:
    """The configured model provider (wrapped to retry transient failures) and its routes."""
    routes = load_routes(settings.llm_model_default, settings.llm_routes_file)
    if settings.llm_provider == "fake":
        return LLMStack(FakeLLMProvider(), routes)
    if settings.anthropic_api_key is None:
        raise ValueError("ANTHROPIC_API_KEY is not set")
    provider = AnthropicProvider(settings.anthropic_api_key.get_secret_value(), routes)
    return LLMStack(RetryingLLMProvider(provider), routes)
