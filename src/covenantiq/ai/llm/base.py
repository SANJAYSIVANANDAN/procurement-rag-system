"""The language-model port (spec 19.15).

Everything that calls a model goes through `LLMProvider`, so the vendor, the model and the effort
level are configuration, and tests run against `FakeLLMProvider` with no network. Only the port and
the fake exist so far (milestone 10 needs them for summarisation); the Anthropic adapter arrives
with the chat graph in milestone 11.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

from covenantiq.core.errors import CovenantError, TransientError

Route = Literal[
    "classify",
    "rewrite",
    "answer",
    "rule_assessment",
    "signal_extraction",
    "explain",
    "narrative",
    "summarize",
    "eval_judge",
]

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(TransientError):
    """A timeout, a rate limit, a 5xx or a connection failure: worth retrying."""


class LLMRefused(CovenantError):
    """The model declined to answer, or its output could not be used. Retrying will not help."""


class LLMTruncated(LLMRefused):
    """The answer hit the token limit, so it is incomplete."""


class LLMBadRequest(CovenantError):
    """The provider rejected the request itself (bad key, unknown model, malformed parameters).
    A configuration or code problem; retrying will not help and the message names no secret."""


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass
class LLMRequest:
    route: Route
    system: str
    messages: list[ChatMessage]
    max_tokens: int
    timeout_s: float
    metadata: dict[str, str] = field(default_factory=dict)  # request id, job id: for logging only


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass
class StructuredResult[T: BaseModel]:
    value: T
    usage: LLMUsage
    model: str


@dataclass(frozen=True)
class LLMStreamEvent:
    kind: Literal["delta", "usage", "stop"]
    text: str = ""
    usage: LLMUsage | None = None
    stop_reason: str | None = None


class LLMProvider(Protocol):
    model_name: str

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Text deltas, then usage, then a stop event."""
        ...

    async def structured(self, request: LLMRequest, schema: type[T]) -> StructuredResult[T]:
        """The model's answer parsed into `schema`."""
        ...

    async def count_tokens(self, request: LLMRequest) -> int: ...
