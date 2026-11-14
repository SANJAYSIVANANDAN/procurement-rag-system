"""Folding old turns into the conversation's rolling summary (spec 23.5).

When the window overflows, the turns that no longer fit are summarised together with the previous
summary, so a long conversation keeps its early context in a bounded space. The model call is routed
through `LLMProvider` (route `summarize`), so tests run against the fake.
"""

from collections.abc import Sequence

from pydantic import BaseModel

from covenantiq.ai.llm.base import ChatMessage, LLMProvider, LLMRefused, LLMRequest
from covenantiq.ai.prompts import load_prompt
from covenantiq.ingestion.chunking.tokens import CHARS_PER_TOKEN
from covenantiq.memory.base import SessionTurn

PROMPT = "conversation_summary.v1"
TIMEOUT_SECONDS = 20.0


class ConversationSummary(BaseModel):
    summary: str


def _escape(text: str) -> str:
    """Keep conversation text from closing the tags that mark it as data."""
    return text.replace("</turns>", "&lt;/turns>").replace(
        "</previous_summary>", "&lt;/previous_summary>"
    )


def render_turns(previous: str | None, turns: Sequence[SessionTurn]) -> str:
    lines = [f"<previous_summary>\n{_escape(previous or '(none)')}\n</previous_summary>", "<turns>"]
    lines.extend(f"[{turn.seq} {turn.role}] {_escape(turn.content)}" for turn in turns)
    lines.append("</turns>")
    return "\n".join(lines)


def fit_to_budget(text: str, max_tokens: int) -> str:
    """Cut at the last sentence end inside the budget (or the last space, failing that)."""
    limit = max_tokens * CHARS_PER_TOKEN
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for mark in (". ", "\n", "; "):
        end = cut.rfind(mark)
        if end > limit // 2:
            return cut[: end + 1].strip()
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + "…"


class Summarizer:
    def __init__(self, llm: LLMProvider, *, summary_tokens: int = 600) -> None:
        self._llm = llm
        self._summary_tokens = summary_tokens

    async def summarize(self, previous: str | None, turns: Sequence[SessionTurn]) -> str:
        """The updated summary. Raises `LLMUnavailable` (worth retrying) or `LLMRefused`."""
        request = LLMRequest(
            route="summarize",
            system=f"{load_prompt(PROMPT)}\n\nLength limit: about {self._summary_tokens} tokens.",
            messages=[ChatMessage("user", render_turns(previous, turns))],
            max_tokens=self._summary_tokens + 100,
            timeout_s=TIMEOUT_SECONDS,
        )
        result = await self._llm.structured(request, ConversationSummary)
        summary = fit_to_budget(result.value.summary, self._summary_tokens)
        if not summary:
            raise LLMRefused("the model returned an empty summary")
        return summary
