from collections.abc import AsyncIterator, Callable
from typing import Any

from pydantic import BaseModel

from covenantiq.ai.llm.base import (
    LLMRequest,
    LLMStreamEvent,
    LLMUnavailable,
    LLMUsage,
    StructuredResult,
    T,
)

Handler = Callable[[LLMRequest], Any]


class FakeLLMProvider:
    """A scriptable provider for tests and offline runs.

    `script(route, handler)` sets what a route answers: a handler receives the request and returns
    either a ready schema instance, a dict of its fields, or text (for the first string field).
    Every request is recorded in `requests`, and `calls` counts how many reached the model.

    Failure injection, for the paths real providers take:
      `fail()`                      every call raises until `heal()`
      `fail_next(n)`                the next n calls raise `LLMUnavailable`, then calls succeed
      `fail_stream_after(words)`    a stream yields that many words, then raises `LLMUnavailable`

    With no script, `summarize` answers with a plain deterministic digest of what it was given and
    other routes answer "ok", so code that calls a model runs offline.
    """

    model_name = "fake-llm"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []
        self.calls = 0
        self._handlers: dict[str, Handler] = {}
        self.fail_with: Exception | None = None
        self._transient_failures = 0
        self._stream_fails_after: int | None = None

    def script(self, route: str, handler: Handler) -> None:
        self._handlers[route] = handler

    def fail(self, error: Exception | None = None) -> None:
        self.fail_with = error or LLMUnavailable("the fake provider is down")

    def heal(self) -> None:
        self.fail_with = None
        self._transient_failures = 0
        self._stream_fails_after = None

    def fail_next(self, count: int) -> None:
        self._transient_failures = count

    def fail_stream_after(self, words: int) -> None:
        self._stream_fails_after = words

    def _begin(self, request: LLMRequest) -> None:
        self.requests.append(request)
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        if self._transient_failures > 0:
            self._transient_failures -= 1
            raise LLMUnavailable("the fake provider failed this call")

    def _answer(self, request: LLMRequest) -> Any:
        handler = self._handlers.get(request.route)
        if handler is not None:
            return handler(request)
        if request.route == "summarize":
            turns = " ".join(m.content for m in request.messages)
            return f"Summary of earlier conversation: {' '.join(turns.split())[:300]}"
        return "ok"

    async def structured(self, request: LLMRequest, schema: type[T]) -> StructuredResult[T]:
        self._begin(request)
        answer = self._answer(request)
        if isinstance(answer, BaseModel):
            value = schema.model_validate(answer.model_dump())
        elif isinstance(answer, dict):
            value = schema.model_validate(answer)
        else:
            first = next(iter(schema.model_fields))
            value = schema.model_validate({first: str(answer)})
        used = LLMUsage(
            input_tokens=sum(len(m.content) for m in request.messages) // 4, output_tokens=10
        )
        return StructuredResult(value=value, usage=used, model=self.model_name)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self._begin(request)
        text = str(self._answer(request))
        for index, word in enumerate(text.split(" ")):
            if self._stream_fails_after is not None and index >= self._stream_fails_after:
                raise LLMUnavailable("the fake stream broke part-way")
            yield LLMStreamEvent("delta", text=word + " ")
        yield LLMStreamEvent("usage", usage=LLMUsage(output_tokens=len(text) // 4))
        yield LLMStreamEvent("stop", stop_reason="end_turn")

    async def count_tokens(self, request: LLMRequest) -> int:
        return (len(request.system) + sum(len(m.content) for m in request.messages)) // 4
