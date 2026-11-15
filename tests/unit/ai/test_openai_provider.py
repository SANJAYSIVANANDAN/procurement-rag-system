"""The OpenAI adapter against a mocked Chat Completions API (respx): no network, no key."""

import json

import httpx
import pytest
import respx
from pydantic import BaseModel, ValidationError

from covenantiq.ai.llm import ChatMessage, LLMRequest, RetryingLLMProvider
from covenantiq.ai.llm.base import (
    LLMBadRequest,
    LLMRefused,
    LLMTruncated,
    LLMUnavailable,
)
from covenantiq.ai.llm.factory import build_llm
from covenantiq.ai.llm.openai_provider import OpenAIProvider
from covenantiq.ai.llm.routes import LLMRoute, RouteTable
from covenantiq.core.config import Settings

BASE = "https://api.test/v1"
URL = f"{BASE}/chat/completions"
KEY = "sk-test-do-not-leak-123456"
ROUTES = RouteTable(
    {
        "answer": LLMRoute(effort="medium", max_tokens=500),
        "classify": LLMRoute(effort="xhigh", max_tokens=100, model="small-model"),
    },
    "big-model",
)


class Intent(BaseModel):
    intent: str
    needs_retrieval: bool = True


def provider(**kw: object) -> OpenAIProvider:
    return OpenAIProvider(KEY, ROUTES, base_url=BASE, **kw)  # type: ignore[arg-type]


def request(route: str = "answer") -> LLMRequest:
    return LLMRequest(
        route=route,  # type: ignore[arg-type]
        system="Be brief.",
        messages=[
            ChatMessage("user", "Hi"),
            ChatMessage("assistant", "Hello"),
            ChatMessage("user", "Cap?"),
        ],
        max_tokens=ROUTES.get(route).max_tokens,
        timeout_s=30,
    )


def sse(*chunks: dict | str) -> httpx.Response:
    lines = [f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks]
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content="".join(lines)
    )


def delta(text: str, finish: str | None = None) -> dict:
    return {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}


USAGE = {
    "choices": [],
    "usage": {
        "prompt_tokens": 40,
        "completion_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 5},
    },
}


async def collect(p: OpenAIProvider, req: LLMRequest) -> list:
    return [event async for event in p.stream(req)]


class TestStreaming:
    @respx.mock
    async def test_text_deltas_then_usage_then_stop(self) -> None:
        respx.post(URL).mock(
            return_value=sse(
                delta("The cap "), delta("is fees."), delta("", "stop"), USAGE, "[DONE]"
            )
        )
        events = await collect(provider(), request())
        assert [e.kind for e in events] == ["delta", "delta", "usage", "stop"]
        assert "".join(e.text for e in events if e.kind == "delta") == "The cap is fees."
        usage = events[2].usage
        assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (40, 7, 5)
        assert events[3].stop_reason == "stop"

    @respx.mock
    async def test_the_request_has_the_current_parameter_names_and_no_sampling(self) -> None:
        route = respx.post(URL).mock(return_value=sse(delta("x", "stop"), USAGE, "[DONE]"))
        await collect(provider(), request())
        sent = json.loads(route.calls.last.request.content)
        assert sent["model"] == "big-model" and sent["stream"] is True
        assert sent["stream_options"] == {"include_usage": True}
        assert sent["max_completion_tokens"] == 500 and "max_tokens" not in sent
        assert not {"temperature", "top_p", "top_k"} & set(sent)
        assert sent["messages"][0] == {"role": "system", "content": "Be brief."}
        assert [m["role"] for m in sent["messages"]] == ["system", "user", "assistant", "user"]
        assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"

    @respx.mock
    async def test_a_route_can_use_another_model_and_effort_is_mapped(self) -> None:
        route = respx.post(URL).mock(return_value=sse(delta("x", "stop"), USAGE, "[DONE]"))
        await collect(provider(), request("classify"))
        sent = json.loads(route.calls.last.request.content)
        assert (
            sent["model"] == "small-model" and sent["reasoning_effort"] == "high"
        )  # xhigh -> high

    @respx.mock
    async def test_effort_is_left_out_for_a_model_that_has_none(self) -> None:
        route = respx.post(URL).mock(return_value=sse(delta("x", "stop"), USAGE, "[DONE]"))
        await collect(provider(send_effort=False), request())
        assert "reasoning_effort" not in json.loads(route.calls.last.request.content)

    @respx.mock
    async def test_a_cut_off_answer_is_a_typed_error_after_its_text(self) -> None:
        respx.post(URL).mock(return_value=sse(delta("half"), delta("", "length"), USAGE, "[DONE]"))
        got = []
        with pytest.raises(LLMTruncated):
            async for event in provider().stream(request()):
                got.append(event)
        assert got[0].text == "half"

    @respx.mock
    async def test_a_content_filter_stop_is_a_refusal(self) -> None:
        respx.post(URL).mock(return_value=sse(delta("", "content_filter"), USAGE, "[DONE]"))
        with pytest.raises(LLMRefused):
            await collect(provider(), request())

    @respx.mock
    async def test_a_refusal_in_the_stream_is_a_refusal(self) -> None:
        refusal = {
            "choices": [{"delta": {"refusal": "I can't help with that."}, "finish_reason": None}]
        }
        respx.post(URL).mock(return_value=sse(refusal, delta("", "stop"), USAGE, "[DONE]"))
        with pytest.raises(LLMRefused):
            await collect(provider(), request())


class TestErrors:
    @pytest.mark.parametrize("status", [408, 409, 429, 500, 503])
    @respx.mock
    async def test_rate_limits_and_server_errors_are_retryable(self, status: int) -> None:
        respx.post(URL).respond(status_code=status, json={"error": {"message": "slow down"}})
        with pytest.raises(LLMUnavailable):
            await collect(provider(), request())

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    @respx.mock
    async def test_a_rejected_request_is_not_retryable_and_names_no_secret(
        self, status: int
    ) -> None:
        respx.post(URL).respond(status_code=status, json={"error": {"message": f"bad key {KEY}"}})
        with pytest.raises(LLMBadRequest) as caught:
            await collect(provider(), request())
        assert KEY not in str(caught.value) and f"HTTP {status}" in str(caught.value)

    @respx.mock
    async def test_a_dropped_connection_is_retryable(self) -> None:
        respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(LLMUnavailable):
            await collect(provider(), request())

    @respx.mock
    async def test_the_retry_wrapper_retries_before_the_first_token(self) -> None:
        route = respx.post(URL).mock(
            side_effect=[
                httpx.Response(429, json={}),
                sse(delta("ok", "stop"), USAGE, "[DONE]"),
            ]
        )

        async def no_sleep(_: float) -> None:
            return None

        wrapped = RetryingLLMProvider(provider(), sleep=no_sleep)
        events = [e async for e in wrapped.stream(request())]
        assert route.call_count == 2 and events[0].text == "ok"


def completion(content: str, finish: str = "stop", **message: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "big-model-2026",
            "choices": [{"message": {"content": content, **message}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        },
    )


class TestStructured:
    @respx.mock
    async def test_the_answer_is_parsed_into_the_schema_with_its_usage(self) -> None:
        route = respx.post(URL).mock(return_value=completion('{"intent": "document_qa"}'))
        result = await provider().structured(request("classify"), Intent)
        assert result.value == Intent(intent="document_qa", needs_retrieval=True)
        assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 3)
        assert result.model == "big-model-2026"
        sent = json.loads(route.calls.last.request.content)
        fmt = sent["response_format"]
        assert fmt["type"] == "json_schema" and fmt["json_schema"]["name"] == "Intent"
        assert fmt["json_schema"]["strict"] is False  # our schemas have optional fields
        assert fmt["json_schema"]["schema"]["properties"]["intent"]["type"] == "string"
        assert "stream" not in sent

    @respx.mock
    async def test_text_that_is_not_the_structure_is_a_refusal_not_a_crash(self) -> None:
        respx.post(URL).mock(return_value=completion("Sure! Here you go."))
        with pytest.raises(LLMRefused):
            await provider().structured(request("classify"), Intent)

    @respx.mock
    async def test_a_cut_off_structure_is_truncated(self) -> None:
        respx.post(URL).mock(return_value=completion('{"intent": "docu', finish="length"))
        with pytest.raises(LLMTruncated):
            await provider().structured(request("classify"), Intent)

    @respx.mock
    async def test_a_refusal_field_is_a_refusal(self) -> None:
        respx.post(URL).mock(return_value=completion("", refusal="No."))
        with pytest.raises(LLMRefused):
            await provider().structured(request("classify"), Intent)

    @respx.mock
    async def test_an_unreadable_body_is_retryable(self) -> None:
        respx.post(URL).respond(status_code=200, text="<html>gateway</html>")
        with pytest.raises(LLMUnavailable):
            await provider().structured(request("classify"), Intent)

    @respx.mock
    async def test_errors_map_like_streaming(self) -> None:
        respx.post(URL).respond(status_code=401, json={})
        with pytest.raises(LLMBadRequest):
            await provider().structured(request("classify"), Intent)


async def test_token_counts_are_an_estimate_not_a_call() -> None:
    with respx.mock(assert_all_called=False) as mock:
        n = await provider().count_tokens(request())
        assert n > 0 and mock.calls.call_count == 0


@pytest.mark.usefixtures("valid_env")
class TestConfiguration:
    @staticmethod
    def settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
        for name, value in {
            "CIQ_OFFLINE": "0",
            "CIQ_EMBEDDING_PROVIDER": "fake",
            "CIQ_RERANK_PROVIDER": "none",
            "CIQ_LLM_PROVIDER": "openai",
            "CIQ_LLM_MODEL_DEFAULT": "some-openai-model",
            **env,
        }.items():
            monkeypatch.setenv(name, value)
        return Settings(_env_file=None)  # type: ignore[call-arg]

    def test_the_provider_needs_a_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("CIQ_OPENAI_COMPAT_API_KEY", raising=False)
        with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
            self.settings(monkeypatch)

    def test_it_is_built_with_the_key_the_model_and_the_retry_wrapper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack = build_llm(self.settings(monkeypatch, OPENAI_API_KEY=KEY))
        assert isinstance(stack.provider, RetryingLLMProvider)
        assert stack.provider.model_name == "some-openai-model"

    def test_the_base_url_can_point_at_another_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack = build_llm(
            self.settings(
                monkeypatch, OPENAI_API_KEY=KEY, CIQ_OPENAI_COMPAT_BASE_URL="http://llm.local/v1"
            )
        )
        inner = stack.provider._inner  # type: ignore[attr-defined]
        assert str(inner._client.base_url) == "http://llm.local/v1/"

    def test_no_anthropic_key_is_needed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert self.settings(monkeypatch, OPENAI_API_KEY=KEY).llm_provider == "openai"
