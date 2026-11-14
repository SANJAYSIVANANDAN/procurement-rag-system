"""The chat graph end to end on fakes: routing, streaming, failure paths and the event order."""

from typing import Any

import pytest

from covenantiq.ai.llm.base import LLMRefused
from covenantiq.core.enums import GroundingStatus, MessageStatus, QueryIntent
from covenantiq.graphs.chat.nodes import rule_intent
from covenantiq.rag.abstention import ABSTENTION_HEADLINE
from covenantiq.streaming.events import SSEEvent
from tests.unit.graphs.harness import (
    DOC,
    GOOD_ANSWER,
    QUESTION,
    SOURCE_TEXT,
    Harness,
    turns,
    weak_retrieval,
)


def kinds(events: list[SSEEvent]) -> list[str]:
    return [e.event for e in events]


def tokens(events: list[SSEEvent]) -> str:
    return "".join(e.text for e in events if e.event == "token")  # type: ignore[union-attr]


def terminal(events: list[SSEEvent]) -> Any:
    assert events[-1].event in ("message_complete", "error")
    return events[-1]


class TestAGroundedAnswer:
    async def test_it_streams_validates_and_stores_the_answer_with_its_citation(self) -> None:
        h = Harness()
        events = await h.ask()

        done = terminal(events)
        assert done.event == "message_complete"
        assert done.status == "completed" and done.grounding_status == "grounded"
        assert done.content == GOOD_ANSWER
        assert kinds(events)[0] == "message_start" and kinds(events)[-1] == "message_complete"
        assert "token" in kinds(events) and kinds(events).count("citation") == 1
        assert tokens(events).strip() == GOOD_ANSWER  # the streamed text is the model's text

        stored = h.outcome
        assert stored.status is MessageStatus.COMPLETED
        assert stored.grounding_status is GroundingStatus.GROUNDED
        assert stored.intent is QueryIntent.DOCUMENT_QA
        assert [c.marker for c in stored.citations] == [1]
        assert stored.citations[0].document_title == "Acme MSA"
        assert stored.citations[0].clause_ref == "6.2"
        assert stored.trace["retrieval"]["rerank_skipped"] is False
        assert stored.trace["evidence"]["level"] in ("strong", "moderate")
        assert stored.usage["output_tokens"] > 0

    async def test_a_status_event_names_each_stage(self) -> None:
        events = await Harness().ask()
        stages = [e.stage for e in events if e.event == "status"]  # type: ignore[union-attr]
        assert stages == ["understanding", "searching", "reading", "writing", "verifying"]

    async def test_the_citation_events_come_after_the_commit_and_before_the_end(self) -> None:
        h = Harness()
        events = await h.ask()
        first_citation = kinds(events).index("citation")
        assert all(k in ("citation",) for k in kinds(events)[first_citation:-1])
        assert h.message_id in h.store.completed

    async def test_retrieval_searches_the_selected_documents_with_the_question(self) -> None:
        h = Harness()
        await h.ask()
        request = h.retriever.requests[0]
        assert list(request.document_ids) == [DOC] and request.query == QUESTION
        assert "liability" in request.keywords

    async def test_the_answer_request_carries_the_sources_and_the_question(self) -> None:
        h = Harness()
        await h.ask()
        sent = h.last_answer_request_messages()[-1].content
        assert '<source id="C1"' in sent and SOURCE_TEXT in sent and QUESTION in sent

    async def test_the_finished_exchange_goes_to_the_memory_window(self) -> None:
        h = Harness()
        await h.ask()
        (turn,) = h.memory.recorded
        assert turn.assistant.content == GOOD_ANSWER and turn.assistant.seq == 2
        assert turn.user.role == "user"


class TestRouting:
    @pytest.mark.parametrize("text", ["hi", "Hello!", "thanks", "Good morning", "thank you."])
    def test_greetings_are_decided_by_rules(self, text: str) -> None:
        assert rule_intent(text) is QueryIntent.SMALLTALK

    @pytest.mark.parametrize("text", ["What does clause 12.2 say?", "show me Section 4(a)"])
    def test_a_named_clause_is_a_lookup(self, text: str) -> None:
        assert rule_intent(text) is QueryIntent.CLAUSE_LOOKUP

    def test_everything_else_needs_the_model(self) -> None:
        assert rule_intent("Who pays for the hosting?") is None

    async def test_smalltalk_gets_a_template_with_no_model_call_and_no_search(self) -> None:
        h = Harness()
        events = await h.ask("hello")
        assert h.llm.calls == 0 and h.retriever.requests == []
        assert terminal(events).grounding_status == "not_applicable"
        assert h.outcome.citations == [] and h.outcome.intent is QueryIntent.SMALLTALK

    @pytest.mark.parametrize(
        "intent", [QueryIntent.OUT_OF_SCOPE, QueryIntent.ANALYSIS_REQUEST, QueryIntent.SMALLTALK]
    )
    async def test_direct_intents_never_search(self, intent: QueryIntent) -> None:
        h = Harness()
        h.llm.script("classify", lambda r: {"intent": intent.value, "is_follow_up": False})
        events = await h.ask("Write me a poem about hosting")
        assert h.retriever.requests == [] and h.routes_called() == ["classify"]
        assert terminal(events).status == "completed"

    async def test_a_question_with_no_documents_selected_abstains_without_searching(self) -> None:
        h = Harness()
        events = await h.ask(documents=[])
        assert h.retriever.requests == [] and "answer" not in h.routes_called()
        done = terminal(events)
        assert done.grounding_status == "abstained"
        assert "no ready documents" in done.content

    async def test_if_the_classifier_fails_it_is_treated_as_a_question_about_the_documents(
        self,
    ) -> None:
        h = Harness()
        h.llm.script("classify", lambda r: {"intent": "banana"})  # not a valid intent
        events = await h.ask()
        assert terminal(events).grounding_status == "grounded"
        assert h.outcome.intent is QueryIntent.DOCUMENT_QA


class TestFollowUps:
    async def test_a_follow_up_is_rewritten_into_a_standalone_query_for_search(self) -> None:
        h = Harness()
        h.memory.context.recent_turns = turns(
            ("What is the liability cap?", "Twelve months of fees.")
        )
        h.llm.script("classify", lambda r: {"intent": "document_qa", "is_follow_up": True})
        h.llm.script(
            "rewrite", lambda r: {"query": "Does the liability cap apply to data breaches?"}
        )
        await h.ask("Does it apply to data breaches?")
        assert h.retriever.requests[0].query == "Does the liability cap apply to data breaches?"
        assert h.routes_called() == ["classify", "rewrite", "answer"]

    async def test_without_history_nothing_is_a_follow_up_and_nothing_is_rewritten(self) -> None:
        h = Harness()
        h.llm.script("classify", lambda r: {"intent": "document_qa", "is_follow_up": True})
        await h.ask("And what about that?")
        assert "rewrite" not in h.routes_called()
        assert h.retriever.requests[0].query == "And what about that?"

    async def test_a_failed_rewrite_searches_with_the_question_as_asked(self) -> None:
        h = Harness()
        h.memory.context.recent_turns = turns(("Q?", "A."))
        h.llm.script("classify", lambda r: {"intent": "document_qa", "is_follow_up": True})
        h.llm.fail_next(0)

        def broken(request: Any) -> Any:
            raise LLMRefused("declined")

        h.llm.script("rewrite", broken)
        events = await h.ask("and the other one?")
        assert terminal(events).event == "message_complete"
        assert h.retriever.requests[0].query == "and the other one?"

    async def test_earlier_turns_and_the_summary_reach_the_answer_call(self) -> None:
        h = Harness()
        h.memory.context.summary = "The user is reviewing the Acme MSA."
        h.memory.context.recent_turns = turns(("Earlier question?", "Earlier answer."))
        await h.ask()
        messages = h.last_answer_request_messages()
        assert [m.role for m in messages] == ["user", "assistant", "user"]
        assert "The user is reviewing the Acme MSA." in messages[-1].content

    async def test_an_unusable_memory_costs_the_history_not_the_answer(self) -> None:
        h = Harness()
        h.memory.fail_load = True
        events = await h.ask()
        assert terminal(events).event == "message_complete"


class TestAbstaining:
    async def test_weak_evidence_abstains_without_asking_the_model_to_answer(self) -> None:
        h = Harness()
        h.retriever.next = weak_retrieval()
        events = await h.ask()
        assert "answer" not in h.routes_called()
        done = terminal(events)
        assert done.grounding_status == "abstained" and ABSTENTION_HEADLINE in done.content
        assert '"Acme MSA"' in done.content  # says what was searched
        assert tokens(events) == done.content  # nothing was streamed before, so it streams now
        assert h.outcome.status is MessageStatus.COMPLETED and h.outcome.citations == []

    async def test_nothing_retrieved_abstains(self) -> None:
        h = Harness()
        h.retriever.next = weak_retrieval()
        h.retriever.next.chunks.clear()
        events = await h.ask()
        assert terminal(events).grounding_status == "abstained"

    async def test_an_answer_with_no_valid_citation_is_replaced_by_the_abstention(self) -> None:
        h = Harness()
        h.llm.script("answer", lambda r: "Liability is capped at one million dollars.")
        events = await h.ask()
        done = terminal(events)
        assert done.grounding_status == "abstained" and ABSTENTION_HEADLINE in done.content
        assert h.outcome.citations == []
        assert tokens(events) != done.content  # the draft was streamed; the final text replaces it

    async def test_an_unsupported_claim_is_marked_not_hidden(self) -> None:
        h = Harness()
        h.llm.script("answer", lambda r: "Termination requires ninety days of notice. [C1]")
        done = terminal(await h.ask())
        assert done.grounding_status in ("ungrounded", "partially_grounded")


class TestFailure:
    async def test_a_model_failure_before_any_token_is_a_failed_message_with_a_safe_error(
        self,
    ) -> None:
        h = Harness()
        h.llm.script("answer", lambda r: GOOD_ANSWER)
        h.llm.fail_stream_after(0)
        events = await h.ask()
        error = terminal(events)
        assert error.event == "error" and error.code == "LLM_UNAVAILABLE" and error.retryable
        assert "fake" not in error.message.lower()
        assert h.outcome.status is MessageStatus.FAILED and h.outcome.content == ""
        assert h.memory.recorded == []  # a failed turn is not remembered

    async def test_a_failure_after_tokens_keeps_the_partial_text_and_does_not_retry(self) -> None:
        h = Harness()
        h.llm.fail_stream_after(4)
        events = await h.ask()
        error = terminal(events)
        assert error.event == "error" and error.retryable
        assert h.outcome.status is MessageStatus.FAILED
        assert h.outcome.content.strip() == tokens(events).strip() != ""
        assert h.llm.calls == 2  # one classify, one answer: the answer was not started again
        assert h.outcome.citations == [] and h.memory.recorded == []

    async def test_a_refusal_is_not_retryable(self) -> None:
        h = Harness()
        h.llm.fail(LLMRefused("declined"))
        h.llm.script("classify", lambda r: (_ for _ in ()).throw(LLMRefused("declined")))
        events = await h.ask()
        assert terminal(events).event == "error" and terminal(events).code == "LLM_REFUSED"
        assert terminal(events).retryable is False

    async def test_a_retrieval_failure_ends_the_run_with_a_retryable_error(self) -> None:
        h = Harness()
        h.retriever.next = ConnectionError("pgvector is down")
        events = await h.ask()
        error = terminal(events)
        assert error.event == "error" and error.retryable
        assert "pgvector" not in error.message  # the detail goes to the log, not to the user
        assert h.outcome.status is MessageStatus.FAILED and "answer" not in h.routes_called()

    async def test_if_the_database_write_fails_the_stream_still_ends_with_one_error(self) -> None:
        h = Harness()
        h.store.fail_complete = True
        events = await h.ask()
        assert terminal(events).code in ("PERSIST_FAILED", "INTERNAL_ERROR")
        assert kinds(events).count("message_complete") == 0

    async def test_an_unexpected_crash_still_ends_the_stream_and_the_message(self) -> None:
        h = Harness()

        async def boom(*_: Any) -> int:
            raise RuntimeError("bug")

        h.store.mark_streaming = boom  # type: ignore[method-assign]
        events = await h.ask()
        # the run never started, but the listener is told, and the message is not left pending
        assert kinds(events) == ["message_start", "error"]  # the order holds even so
        assert terminal(events).code == "INTERNAL_ERROR"

    async def test_a_model_failure_before_the_first_token_is_retried_by_the_provider(self) -> None:
        h = Harness(retrying=True)
        h.llm.fail_next(1)
        events = await h.ask("What does clause 6.2 say about liability?")  # no classify call
        assert terminal(events).event == "message_complete"
        assert h.llm.calls == 2  # the answer failed once and was started again, before any token
        assert tokens(events).strip() == GOOD_ANSWER  # and the user never saw the failed attempt

    async def test_the_graph_itself_does_not_retry_so_a_bare_provider_failure_is_final(
        self,
    ) -> None:
        h = Harness()
        h.llm.fail_next(1)
        events = await h.ask("What does clause 6.2 say about liability?")
        assert terminal(events).event == "error" and h.llm.calls == 1


class TestMemoryAfterTheCommit:
    async def test_a_redis_failure_after_the_commit_drops_the_session_and_still_completes(
        self,
    ) -> None:
        h = Harness()
        h.memory.fail_record = True
        events = await h.ask()
        assert terminal(events).event == "message_complete"
        assert h.outcome.status is MessageStatus.COMPLETED  # PostgreSQL already has the reply
        assert h.memory.invalidated == 1

    async def test_an_abstention_is_remembered_like_any_other_completed_reply(self) -> None:
        h = Harness()
        h.retriever.next = weak_retrieval()
        await h.ask()
        assert len(h.memory.recorded) == 1
