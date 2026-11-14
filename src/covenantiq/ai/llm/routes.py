"""Per-route model configuration, loaded from `config/llm_routes.yaml` (spec 19.15)."""

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field

from covenantiq.ai.llm.base import ChatMessage, LLMRequest, Route

Effort = Literal["low", "medium", "high", "xhigh", "max"]
DEFAULT_ROUTES_PATH = Path("config/llm_routes.yaml")


class LLMRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | None = None  # None: the default model from settings
    effort: Effort = "medium"
    max_tokens: Annotated[int, Field(gt=0, le=128_000)] = 2048
    timeout_s: Annotated[float, Field(gt=0)] = 60.0


class RoutesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    routes: dict[str, LLMRoute] = Field(default_factory=dict)


class RouteTable:
    """Looks up how a route should call the model, and builds requests that carry those budgets."""

    def __init__(self, routes: dict[str, LLMRoute], default_model: str) -> None:
        unknown = set(routes) - set(get_args(Route))
        if unknown:
            raise ValueError(
                f"unknown LLM route(s) in the routes file: {', '.join(sorted(unknown))}"
            )
        self._routes = routes
        self._default_model = default_model

    def get(self, route: str) -> LLMRoute:
        return self._routes.get(route) or LLMRoute()

    def model_for(self, route: str) -> str:
        return self.get(route).model or self._default_model

    def request(
        self,
        route: Route,
        system: str,
        messages: Sequence[ChatMessage],
        *,
        metadata: dict[str, str] | None = None,
    ) -> LLMRequest:
        """A request with the route's token and time budgets."""
        config = self.get(route)
        return LLMRequest(
            route=route,
            system=system,
            messages=list(messages),
            max_tokens=config.max_tokens,
            timeout_s=config.timeout_s,
            metadata=metadata or {},
        )


def load_routes(default_model: str, path: Path | None = None) -> RouteTable:
    """The routes file, or the built-in defaults if it does not exist. A malformed file, or a route
    name that does not exist, is an error at start-up rather than a silently ignored setting."""
    target = path or DEFAULT_ROUTES_PATH
    routes: dict[str, LLMRoute] = {}
    if target.is_file():
        with target.open(encoding="utf-8") as handle:
            routes = RoutesFile.model_validate(yaml.safe_load(handle) or {}).routes
    return RouteTable(routes, default_model)
