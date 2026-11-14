import pytest
from pydantic import BaseModel

from covenantiq.ai.llm import (
    ChatMessage,
    FakeLLMProvider,
    LLMBadRequest,
    LLMRefused,
    LLMUnavailable,
    RetryingLLMProvider,
)
from covenantiq.ai.llm.base import LLMRequest


class Reply(BaseModel):
    text: str


def request(route: str = "answer") -> LLMRequest:
    return LLMRequest(route, "system", [ChatMessage("user", "hi")], 100, 5.0)  # type: ignore[arg-type]


def wrap(fake: FakeLLMProvider, retries: int = 2) -> tuple[RetryingLLMProvider, list[float]]:
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    return RetryingLLMProvider(fake, retries=retries, sleep=sleep), delays


async def collect(llm: RetryingLLMProvider) -> str:
    return "".join([e.text async for e in llm.stream(request()) if e.kind == "delta"]).strip()


class TestStreamRetries:
    async def test_a_failure_before_the_first_token_is_retried_and_the_text_is_not_repeated(
        self,
    ) -> None:
        fake = FakeLLMProvider()
        fake.script("answer", lambda r: "one two three")
        fake.fail_next(2)
        llm, delays = wrap(fake)
        assert await collect(llm) == "one two three"
        assert fake.calls == 3
        assert len(delays) == 2 and delays[1] > delays[0] * 0.9  # backoff grows

    async def test_a_failure_after_the_first_token_is_not_retried(self) -> None:
        """A retry would repeat text the user has already seen, so the stream just fails."""
        fake = FakeLLMProvider()
        fake.script("answer", lambda r: "one two three four")
        fake.fail_stream_after(2)
        llm, delays = wrap(fake)
        seen: list[str] = []
        with pytest.raises(LLMUnavailable):
            async for event in llm.stream(request()):
                if event.kind == "delta":
                    seen.append(event.text)
        assert seen == ["one ", "two "]
        assert fake.calls == 1 and delays == []

    async def test_it_gives_up_after_the_configured_retries(self) -> None:
        fake = FakeLLMProvider()
        fake.fail()
        llm, delays = wrap(fake, retries=2)
        with pytest.raises(LLMUnavailable):
            await collect(llm)
        assert fake.calls == 3 and len(delays) == 2

    @pytest.mark.parametrize("error", [LLMRefused("declined"), LLMBadRequest("bad key")])
    async def test_refusals_and_rejected_requests_are_never_retried(self, error: Exception) -> None:
        fake = FakeLLMProvider()
        fake.fail(error)
        llm, delays = wrap(fake)
        with pytest.raises(type(error)):
            await collect(llm)
        assert fake.calls == 1 and delays == []


class TestStructuredRetries:
    async def test_transient_failures_are_retried(self) -> None:
        fake = FakeLLMProvider()
        fake.script("classify", lambda r: {"text": "fine"})
        fake.fail_next(1)
        llm, _ = wrap(fake)
        result = await llm.structured(request("classify"), Reply)
        assert result.value.text == "fine" and fake.calls == 2

    async def test_permanent_failures_are_not(self) -> None:
        fake = FakeLLMProvider()
        fake.fail(LLMBadRequest("rejected"))
        llm, _ = wrap(fake)
        with pytest.raises(LLMBadRequest):
            await llm.structured(request("classify"), Reply)
        assert fake.calls == 1


async def test_metadata_and_token_counting_pass_through() -> None:
    llm, _ = wrap(FakeLLMProvider())
    assert llm.model_name == "fake-llm"
    assert await llm.count_tokens(request()) > 0
