"""The parts of the full run that need no paid model: the RAGAS adapter, which samples are
judged, and the refusal to produce a "full" report from fake providers."""

import pytest

pytest.importorskip("ragas")  # the `eval` dependency group; the offline run does not need it

from langchain_core.prompt_values import StringPromptValue

from covenantiq.ai.embeddings.fake import HashEmbeddingProvider
from covenantiq.ai.llm.fake import FakeLLMProvider
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.core.config import Settings
from eval.dataset import ChatExample, Passage
from eval.pipeline import ChatRun
from eval.ragas_adapter import (
    METRIC_NAMES,
    ProviderRagasEmbeddings,
    ProviderRagasLLM,
    build_samples,
    ragas_metrics,
    run_ragas,
)
from eval.scripts.run_full import TARGETS, NotARealRun, require_real_providers, target_lines


def example(answerable: bool = True, **kw: object) -> ChatExample:
    fields: dict[str, object] = {
        "id": "qa-1",
        "document_refs": ["a.txt"],
        "question": "How is liability limited?",
        "answerable": answerable,
        "ground_truth_answer": "Capped at fees." if answerable else "",
        "relevant_passages": [Passage(document="a.txt", text="The cap is the fees paid.")]
        if answerable
        else [],
        "expected_citations": [],
    }
    fields.update(kw)
    return ChatExample(**fields)  # type: ignore[arg-type]


def run(**kw: object) -> ChatRun:
    fields: dict[str, object] = {
        "example_id": "qa-1",
        "content": "Liability is capped at the fees paid.",
        "grounding": "grounded",
        "retrieved": ["The cap is the fees paid.", "Other text."],
    }
    fields.update(kw)
    return ChatRun(**fields)  # type: ignore[arg-type]


class TestTheAdapter:
    async def test_the_judge_is_our_configured_model(self) -> None:
        fake = FakeLLMProvider()
        fake.script("eval_judge", lambda request: f"verdict for: {request.messages[0].content}")
        judge = ProviderRagasLLM(fake, RouteTable({}, "m"))

        result = await judge.agenerate_text(StringPromptValue(text="is it faithful?"))

        assert result.generations[0][0].text.strip().startswith("verdict for: is it faithful?")
        assert fake.requests[0].route == "eval_judge"

    async def test_several_completions_are_several_calls(self) -> None:
        fake = FakeLLMProvider()
        fake.script("eval_judge", lambda request: "ok")
        result = await ProviderRagasLLM(fake, RouteTable({}, "m")).agenerate_text(
            StringPromptValue(text="x"), n=3
        )
        assert len(result.generations[0]) == 3 and fake.calls == 3

    async def test_the_synchronous_entry_points_work_inside_a_running_loop(self) -> None:
        fake = FakeLLMProvider()
        fake.script("eval_judge", lambda request: "sync answer")
        judge = ProviderRagasLLM(fake, RouteTable({}, "m"))
        assert (
            "sync answer" in judge.generate_text(StringPromptValue(text="x")).generations[0][0].text
        )
        embedder = ProviderRagasEmbeddings(HashEmbeddingProvider())
        assert len(embedder.embed_query("liability")) == 1024
        assert len(embedder.embed_documents(["a", "b"])) == 2

    async def test_the_embeddings_are_our_configured_provider(self) -> None:
        provider = HashEmbeddingProvider()
        wrapped = ProviderRagasEmbeddings(provider)
        assert await wrapped.aembed_query("liability cap") == await provider.embed_query(
            "liability cap"
        )
        assert len(await wrapped.aembed_documents(["x", "y", "z"])) == 3

    def test_the_judge_never_reports_a_truncated_response(self) -> None:
        judge = ProviderRagasLLM(FakeLLMProvider(), RouteTable({}, "m"))
        assert judge.is_finished(object()) is True  # type: ignore[arg-type]


class TestWhichAnswersAreJudged:
    def test_an_answered_answerable_question_becomes_a_sample(self) -> None:
        (sample,) = build_samples([(example(), run())])
        assert sample.user_input == "How is liability limited?"
        assert sample.response == "Liability is capped at the fees paid."
        assert sample.retrieved_contexts == ["The cap is the fees paid.", "Other text."]
        assert sample.reference == "Capped at fees." and sample.reference_contexts == [
            "The cap is the fees paid."
        ]

    @pytest.mark.parametrize(
        "excluded",
        [
            (example(answerable=False), run()),  # nothing to compare an answer with
            (example(), run(grounding="abstained")),  # there is no answer to judge
            (example(), run(content="")),
            (example(), run(error="boom")),
        ],
    )
    def test_what_has_nothing_to_judge_is_left_out(
        self, excluded: tuple[ChatExample, ChatRun]
    ) -> None:
        assert build_samples([excluded]) == []

    def test_an_ungrounded_answer_is_still_judged(self) -> None:
        # it was shown to the user, so its faithfulness is exactly what the metric is for
        assert len(build_samples([(example(), run(grounding="ungrounded"))])) == 1

    async def test_with_nothing_to_judge_no_model_is_called(self) -> None:
        fake = FakeLLMProvider()
        assert run_ragas([], fake, RouteTable({}, "m"), HashEmbeddingProvider()) == {}
        assert fake.calls == 0


class TestTheMetrics:
    def test_every_spec_metric_is_there_with_its_reporting_name(self) -> None:
        metrics = ragas_metrics()
        assert len(metrics) == 6
        assert {m.name for m in metrics} == set(METRIC_NAMES)
        assert set(METRIC_NAMES.values()) == {
            "ragas_faithfulness",
            "ragas_answer_relevancy",
            "ragas_context_precision_llm",
            "ragas_context_recall_llm",
            "ragas_context_precision_nonllm",
            "ragas_context_recall_nonllm",
        }


class TestRefusingToFakeAFullRun:
    def settings(self, monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
        for name, value in {
            "CIQ_ENV": "test",
            "CIQ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost/d",
            "CIQ_CHECKPOINT_DB_URL": "postgresql://u:p@localhost/d",
            "CIQ_REDIS_URL": "redis://localhost:6379/0",
            "CIQ_CELERY_RESULT_BACKEND": "redis://localhost:6379/1",
            "CIQ_RABBITMQ_URL": "amqp://u:p@localhost:5672/v",
            "CIQ_JWT_SECRET": "x" * 40,
            **env,
        }.items():
            monkeypatch.setenv(name, value)
        return Settings(_env_file=None)  # type: ignore[call-arg]

    @pytest.mark.usefixtures("valid_env")
    def test_offline_settings_are_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(NotARealRun, match="CIQ_OFFLINE"):
            require_real_providers(self.settings(monkeypatch, CIQ_OFFLINE="1"))

    @pytest.mark.usefixtures("valid_env")
    def test_a_fake_model_or_fake_embeddings_are_refused_by_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = self.settings(
            monkeypatch,
            CIQ_OFFLINE="0",
            CIQ_LLM_PROVIDER="fake",
            CIQ_EMBEDDING_PROVIDER="fake",
            CIQ_RERANK_PROVIDER="none",
        )
        with pytest.raises(NotARealRun) as caught:
            require_real_providers(settings)
        assert "CIQ_LLM_PROVIDER" in str(caught.value) and "CIQ_EMBEDDING_PROVIDER" in str(
            caught.value
        )

    @pytest.mark.usefixtures("valid_env")
    def test_real_providers_are_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = self.settings(
            monkeypatch,
            CIQ_OFFLINE="0",
            CIQ_LLM_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="sk-test",
            CIQ_EMBEDDING_PROVIDER="openai_compat",
            OPENAI_API_KEY="sk-test",
            CIQ_OPENAI_COMPAT_BASE_URL="https://api.example.com/v1",
            CIQ_RERANK_PROVIDER="none",
        )
        require_real_providers(settings)  # does not raise


class TestTargets:
    def test_the_targets_are_the_specs(self) -> None:
        assert TARGETS["ragas_faithfulness"] == (">=", 0.85)
        assert TARGETS["unsupported_claim_rate"] == ("<=", 0.10)
        assert TARGETS["vendor_scoring_determinism"] == (">=", 1.0)

    def test_the_summary_says_met_not_met_and_not_measured(self) -> None:
        lines = target_lines(
            {
                "ragas_faithfulness": {"value": 0.90},
                "unsupported_claim_rate": {"value": 0.30},
                "abstention_accuracy": {"value": None},
            }
        )
        text = "\n".join(lines)
        assert "ragas_faithfulness: 0.900 (target >= 0.85) met" in text
        assert "unsupported_claim_rate: 0.300 (target <= 0.1) NOT MET" in text
        assert (
            "abstention_accuracy: not measured" in text
            and "ragas_answer_relevancy: not measured" in text
        )
