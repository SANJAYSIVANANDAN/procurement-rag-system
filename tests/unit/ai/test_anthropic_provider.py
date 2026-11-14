"""The Anthropic adapter against canned HTTP responses.

The SDK runs on `httpx2`, so the transport here is `httpx2.MockTransport` (the `respx` the spec
names mocks `httpx` and would not see these calls). The real SDK parses real wire formats: server-sent
event frames for streaming, a JSON message for structured output.
"""

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from covenantiq.ai.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider, map_error
from covenantiq.ai.llm.base import (
    ChatMessage,
    LLMBadRequest,
    LLMRefused,
    LLMRequest,
    LLMTruncated,
    LLMUnavailable,
)
from covenantiq.ai.llm.routes import LLMRoute, RouteTable

SECRET = "sk-ant-secret-value-123456"


class Verdict(BaseModel):
    answer: str
    confident: bool


def sse(*events: tuple[str, dict[str, Any]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def stream_body(
    words: list[str], *, stop_reason: str = "end_turn", input_tokens: int = 25, output: int = 15
) -> bytes:
    start = {
        "type": "message_start",
        "message": {
            "id": "msg_01", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 1, "cache_read_input_tokens": 7},
        },
    }  # fmt: skip
    events: list[tuple[str, dict[str, Any]]] = [
        ("message_start", start),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ]  # fmt: skip
    for word in words:
        events.append(
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": word}})
        )  # fmt: skip
    events += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": output}}),
        ("message_stop", {"type": "message_stop"}),
    ]  # fmt: skip
    return sse(*events)


def message_json(text: str, *, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_01", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 6},
    }  # fmt: skip


class Server:
    """Answers HTTP requests with a canned reply and remembers what was asked."""

    def __init__(self, reply: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.reply = reply
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.reply(request)

    @property
    def body(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[-1].content)
        return loaded


def provider(server: Server, routes: RouteTable | None = None, **kwargs: Any) -> AnthropicProvider:
    client = anthropic.AsyncAnthropic(
        api_key=SECRET,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(server)),
    )
    table = routes or RouteTable(
        {
            "answer": LLMRoute(effort="medium", max_tokens=2048, timeout_s=60),
            "classify": LLMRoute(effort="low", max_tokens=256, timeout_s=4),
            "rewrite": LLMRoute(model="claude-haiku-5-5", effort="low"),
        },
        default_model="claude-opus-5-5",
    )
    return AnthropicProvider(SECRET, table, client=client, **kwargs)


def request(route: str = "answer", text: str = "What is the cap?") -> LLMRequest:
    return LLMRequest(
        route, "You are a contracts assistant.", [ChatMessage("user", text)], 2048, 60.0
    )  # type: ignore[arg-type]


def streaming(words: list[str], **kwargs: Any) -> Server:
    return Server(
        lambda r: httpx2.Response(
            200, content=stream_body(words, **kwargs), headers={"content-type": "text/event-stream"}
        )
    )


def json_reply(body: dict[str, Any], status: int = 200) -> Server:
    return Server(lambda r: httpx2.Response(status, json=body))


class TestStreaming:
    async def test_text_arrives_as_deltas_then_usage_then_stop(self) -> None:
        server = streaming(["The cap ", "is twelve ", "months."])
        events = [e async for e in provider(server).stream(request())]
        assert [e.text for e in events if e.kind == "delta"] == [
            "The cap ",
            "is twelve ",
            "months.",
        ]
        assert [e.kind for e in events][-2:] == ["usage", "stop"]
        usage = next(e.usage for e in events if e.kind == "usage")
        assert usage is not None
        assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (25, 15, 7)
        assert events[-1].stop_reason == "end_turn"

    async def test_the_request_follows_the_current_api_rules(self) -> None:
        server = streaming(["ok"])
        _ = [e async for e in provider(server).stream(request("answer"))]
        http, body = server.requests[-1], server.body

        assert http.url.path == "/v1/messages"
        assert body["model"] == "claude-opus-5-5"
        assert body["stream"] is True
        assert body["max_tokens"] == 2048
        assert body["system"] == "You are a contracts assistant."
        assert body["messages"] == [{"role": "user", "content": "What is the cap?"}]
        assert body["output_config"]["effort"] == "medium"
        for forbidden in ("temperature", "top_p", "top_k", "thinking"):
            assert forbidden not in body  # rejected or unnecessary on current models
        assert FALLBACK_BETA in http.headers["anthropic-beta"]
        assert body["fallbacks"] == "default"
        assert http.headers["authorization"] == f"Bearer {SECRET}"

    async def test_each_route_uses_its_own_model_and_effort(self) -> None:
        server = streaming(["ok"])
        llm = provider(server)
        _ = [e async for e in llm.stream(request("classify"))]
        assert (server.body["model"], server.body["output_config"]["effort"]) == (
            "claude-opus-5-5",
            "low",
        )
        _ = [e async for e in llm.stream(request("rewrite"))]
        assert server.body["model"] == "claude-haiku-5-5"

    async def test_the_fallback_can_be_switched_off(self) -> None:
        server = streaming(["ok"])
        _ = [e async for e in provider(server, fallbacks=False).stream(request())]
        assert "fallbacks" not in server.body
        assert "server-side-fallback" not in server.requests[-1].headers.get("anthropic-beta", "")

    async def test_a_refusal_ends_the_stream_with_an_error_after_what_was_sent(self) -> None:
        server = streaming(["I can't "], stop_reason="refusal")
        seen: list[str] = []
        with pytest.raises(LLMRefused, match="declined"):
            async for event in provider(server).stream(request()):
                if event.kind == "delta":
                    seen.append(event.text)
        assert seen == ["I can't "]

    async def test_hitting_the_token_limit_is_a_truncation_error(self) -> None:
        server = streaming(["The cap is"], stop_reason="max_tokens")
        with pytest.raises(LLMTruncated, match="2048 tokens"):
            _ = [e async for e in provider(server).stream(request())]

    async def test_the_sdk_does_not_retry_on_its_own(self) -> None:
        server = Server(
            lambda r: httpx2.Response(
                503, json={"type": "error", "error": {"type": "api_error", "message": "x"}}
            )
        )
        with pytest.raises(LLMUnavailable):
            _ = [e async for e in provider(server).stream(request())]
        assert len(server.requests) == 1  # retrying is RetryingLLMProvider's job


class TestStructured:
    async def test_the_answer_is_parsed_into_the_schema(self) -> None:
        server = json_reply(message_json('{"answer": "twelve months", "confident": true}'))
        result = await provider(server).structured(request("classify"), Verdict)
        assert result.value == Verdict(answer="twelve months", confident=True)
        assert (result.usage.input_tokens, result.usage.output_tokens) == (11, 6)
        assert result.model == "claude-opus-5-5"

    async def test_the_request_names_the_schema_the_effort_and_the_fallback(self) -> None:
        server = json_reply(message_json('{"answer": "a", "confident": false}'))
        await provider(server).structured(request("classify"), Verdict)
        body = server.body
        assert "stream" not in body or body["stream"] is False
        assert body["output_config"]["effort"] == "low"
        assert body["output_config"]["format"]["type"] == "json_schema"
        assert set(body["output_config"]["format"]["schema"]["properties"]) == {
            "answer",
            "confident",
        }
        assert "temperature" not in body and body["fallbacks"] == "default"

    async def test_a_refusal_is_an_error_not_an_empty_answer(self) -> None:
        server = json_reply({**message_json(""), "content": [], "stop_reason": "refusal"})
        with pytest.raises(LLMRefused):
            await provider(server).structured(request("classify"), Verdict)

    async def test_a_cut_off_answer_is_a_typed_error_not_a_parse_crash(self) -> None:
        server = json_reply(message_json('{"answer": "twel', stop_reason="max_tokens"))
        with pytest.raises(LLMRefused, match="cut off at 2048 tokens"):
            await provider(server).structured(request("classify"), Verdict)

    async def test_text_that_is_not_the_schema_is_a_typed_error(self) -> None:
        server = json_reply(message_json("I think the answer is twelve months."))
        with pytest.raises(LLMRefused, match="could not be read"):
            await provider(server).structured(request("classify"), Verdict)


class TestErrorMapping:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
    async def test_rate_limits_and_server_errors_are_retryable(self, status: int) -> None:
        server = Server(
            lambda r: httpx2.Response(
                status,
                json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
            )
        )
        with pytest.raises(LLMUnavailable):
            await provider(server).structured(request("classify"), Verdict)

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    async def test_a_rejected_request_is_not_retryable(self, status: int) -> None:
        server = Server(
            lambda r: httpx2.Response(
                status,
                json={
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "model: bad"},
                },
            )
        )
        with pytest.raises(LLMBadRequest) as caught:
            await provider(server).structured(request("classify"), Verdict)
        assert f"HTTP {status}" in str(caught.value)

    async def test_a_connection_failure_is_retryable(self) -> None:
        def boom(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("refused")

        with pytest.raises(LLMUnavailable):
            await provider(Server(boom)).structured(request("classify"), Verdict)

    async def test_a_timeout_is_retryable(self) -> None:
        def slow(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("slow")

        with pytest.raises(LLMUnavailable):
            _ = [e async for e in provider(Server(slow)).stream(request())]

    @pytest.mark.parametrize("status", [401, 403, 400, 500])
    async def test_neither_the_key_nor_the_prompt_ever_appears_in_an_error(
        self, status: int
    ) -> None:
        server = Server(
            lambda r: httpx2.Response(
                status,
                json={
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": f"bad key {SECRET} for What is the cap?",
                    },
                },
            )
        )
        with pytest.raises((LLMUnavailable, LLMBadRequest)) as caught:
            await provider(server).structured(request("classify", "What is the cap?"), Verdict)
        assert SECRET not in str(caught.value) and "What is the cap" not in str(caught.value)
        assert caught.value.__cause__ is None  # the SDK exception, which holds the body, is dropped

    def test_a_status_error_maps_by_code(self) -> None:
        response = httpx2.Response(418, request=httpx2.Request("POST", "https://x"))
        error = anthropic.APIStatusError("teapot", response=response, body=None)
        assert isinstance(map_error(error), LLMBadRequest)


class TestCountTokens:
    async def test_the_count_comes_from_the_provider(self) -> None:
        server = json_reply({"input_tokens": 321})
        assert await provider(server).count_tokens(request()) == 321
        assert server.requests[-1].url.path == "/v1/messages/count_tokens"


class TestRoutes:
    def test_unknown_route_names_are_refused_at_start_up(self) -> None:
        with pytest.raises(ValueError, match="unknown LLM route"):
            RouteTable({"chat": LLMRoute()}, "claude-opus-5-5")

    def test_requests_carry_the_route_budgets(self) -> None:
        table = RouteTable({"classify": LLMRoute(effort="low", max_tokens=256, timeout_s=4)}, "m")
        made = table.request(
            "classify", "system", [ChatMessage("user", "hi")], metadata={"request_id": "r1"}
        )
        assert (made.max_tokens, made.timeout_s, made.metadata) == (256, 4, {"request_id": "r1"})

    def test_a_route_without_config_gets_defaults_and_the_default_model(self) -> None:
        table = RouteTable({}, "claude-opus-5-5")
        assert table.model_for("summarize") == "claude-opus-5-5"
        assert table.get("summarize").effort == "medium"
