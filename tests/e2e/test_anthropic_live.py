"""The Anthropic adapter against the real API. Opt in with CIQ_E2E_LLM=1 and ANTHROPIC_API_KEY set.

These calls cost a few hundred tokens in total. They check what the mocked tests cannot: that the
request shape this project sends (effort, the refusal-fallback beta, no sampling parameters) is
accepted by the live API, that streaming and structured output work, and that real usage is
reported. They have not been run on this machine, which has no key.
"""

import os

import pytest

from covenantiq.ai.llm import ChatMessage, RetryingLLMProvider
from covenantiq.ai.llm.anthropic_provider import AnthropicProvider
from covenantiq.ai.llm.routes import LLMRoute, RouteTable
from covenantiq.ai.schemas import IntentClassification
from covenantiq.core.enums import QueryIntent

pytestmark = pytest.mark.skipif(
    os.environ.get("CIQ_E2E_LLM") != "1" or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="set CIQ_E2E_LLM=1 and ANTHROPIC_API_KEY to call the real API",
)

ROUTES = RouteTable(
    {
        "classify": LLMRoute(effort="low", max_tokens=256, timeout_s=30),
        "answer": LLMRoute(effort="low", max_tokens=200, timeout_s=60),
    },
    "claude-opus-5-5",
)


@pytest.fixture
def llm() -> RetryingLLMProvider:
    return RetryingLLMProvider(AnthropicProvider(os.environ["ANTHROPIC_API_KEY"], ROUTES))


async def test_a_streamed_answer_has_text_usage_and_a_normal_stop(llm: RetryingLLMProvider) -> None:
    request = ROUTES.request(
        "answer",
        "Answer in one short sentence.",
        [ChatMessage("user", "What is a force majeure clause?")],
    )
    events = [event async for event in llm.stream(request)]
    text = "".join(e.text for e in events if e.kind == "delta")
    assert len(text.split()) > 3
    usage = next(e.usage for e in events if e.kind == "usage")
    assert usage is not None and usage.input_tokens > 0 and usage.output_tokens > 0
    assert events[-1].kind == "stop" and events[-1].stop_reason == "end_turn"


async def test_structured_output_is_parsed_into_the_schema(llm: RetryingLLMProvider) -> None:
    request = ROUTES.request(
        "classify",
        "Classify the message. intents: document_qa, clause_lookup, comparison_qa, summary, "
        "analysis_request, smalltalk, out_of_scope.",
        [ChatMessage("user", "What is the notice period for termination?")],
    )
    result = await llm.structured(request, IntentClassification)
    assert result.value.intent in (QueryIntent.DOCUMENT_QA, QueryIntent.CLAUSE_LOOKUP)
    assert result.usage.output_tokens > 0


async def test_token_counting_matches_the_provider(llm: RetryingLLMProvider) -> None:
    request = ROUTES.request("answer", "Be brief.", [ChatMessage("user", "Hello there.")])
    assert await llm.count_tokens(request) > 0
