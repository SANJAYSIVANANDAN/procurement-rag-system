"""RAGAS on our own providers (spec 28.2). Used only by the full run.

RAGAS 0.4 judges with an LLM and, for answer relevancy, an embedding model. This adapter presents
our `LLMProvider` and `EmbeddingProvider` to it, so the judge is whatever the project is configured
to use and no second set of credentials is needed. Everything RAGAS-specific is confined to this
file: a change in a later RAGAS release is fixed here, nowhere else. The version is pinned in the
`eval` dependency group.

Importing this module needs the group (`uv sync --group eval`); the offline run does not.
"""

import asyncio
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

from langchain_core.outputs import Generation, LLMResult
from ragas import EvaluationDataset, SingleTurnSample, evaluate
from ragas.embeddings.base import BaseRagasEmbeddings
from ragas.llms.base import BaseRagasLLM
from ragas.metrics import (
    Faithfulness,
    LLMContextPrecisionWithReference,
    LLMContextRecall,
    NonLLMContextPrecisionWithReference,
    NonLLMContextRecall,
    ResponseRelevancy,
)

from covenantiq.ai.embeddings.base import EmbeddingProvider
from covenantiq.ai.llm.base import ChatMessage, LLMProvider
from covenantiq.ai.llm.routes import RouteTable
from eval.dataset import ChatExample
from eval.pipeline import ChatRun

JUDGE_SYSTEM = (
    "You are a careful evaluation assistant. Follow the instructions in the user message exactly."
)
METRIC_NAMES = {
    "faithfulness": "ragas_faithfulness",
    "answer_relevancy": "ragas_answer_relevancy",
    "llm_context_precision_with_reference": "ragas_context_precision_llm",
    "context_recall": "ragas_context_recall_llm",
    "non_llm_context_precision_with_reference": "ragas_context_precision_nonllm",
    "non_llm_context_recall": "ragas_context_recall_nonllm",
}


def _run_sync(coroutine: Any) -> Any:
    """Run a coroutine from synchronous code, whether or not a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


class ProviderRagasLLM(BaseRagasLLM):
    """A RAGAS judge that calls our configured model."""

    def __init__(self, llm: LLMProvider, routes: RouteTable, *, route: str = "eval_judge") -> None:
        super().__init__()
        self._llm = llm
        self._routes = routes
        self._route = route

    async def _complete(self, prompt: str) -> str:
        request = self._routes.request(self._route, JUDGE_SYSTEM, [ChatMessage("user", prompt)])  # type: ignore[arg-type]
        parts: list[str] = []
        async for event in self._llm.stream(request):
            if event.kind == "delta":
                parts.append(event.text)
        return "".join(parts)

    async def agenerate_text(
        self,
        prompt: Any,
        n: int = 1,
        temperature: float | None = None,
        stop: list[str] | None = None,
        callbacks: Any = None,
    ) -> LLMResult:
        # `temperature` and `stop` are not passed on: the current models take neither.
        texts = [await self._complete(prompt.to_string()) for _ in range(max(1, n))]
        return LLMResult(generations=[[Generation(text=t) for t in texts]])

    def generate_text(
        self,
        prompt: Any,
        n: int = 1,
        temperature: float = 0.01,
        stop: list[str] | None = None,
        callbacks: Any = None,
    ) -> LLMResult:
        result: LLMResult = _run_sync(self.agenerate_text(prompt, n, temperature, stop, callbacks))
        return result

    def is_finished(self, response: LLMResult) -> bool:
        return True


class ProviderRagasEmbeddings(BaseRagasEmbeddings):
    """RAGAS embeddings from our configured embedding provider."""

    def __init__(self, embedder: EmbeddingProvider) -> None:
        super().__init__()
        self._embedder = embedder

    async def aembed_query(self, text: str) -> list[float]:
        return await self._embedder.embed_query(text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embedder.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        result: list[float] = _run_sync(self.aembed_query(text))
        return result

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = _run_sync(self.aembed_documents(texts))
        return result


def build_samples(pairs: Sequence[tuple[ChatExample, ChatRun]]) -> list[SingleTurnSample]:
    """RAGAS samples from the answerable questions that were actually answered: an abstention has
    no response to judge (it is measured by the abstention metrics instead)."""
    return [
        SingleTurnSample(
            user_input=example.question,
            retrieved_contexts=list(run.retrieved),
            response=run.content,
            reference=example.ground_truth_answer,
            reference_contexts=[p.text for p in example.relevant_passages],
        )
        for example, run in pairs
        if example.answerable and run.content and not run.abstained and not run.error
    ]


def ragas_metrics() -> list[Any]:
    return [
        Faithfulness(),
        ResponseRelevancy(),
        LLMContextPrecisionWithReference(),
        LLMContextRecall(),
        NonLLMContextPrecisionWithReference(),
        NonLLMContextRecall(),
    ]


def run_ragas(
    samples: Sequence[SingleTurnSample],
    llm: LLMProvider,
    routes: RouteTable,
    embedder: EmbeddingProvider,
) -> dict[str, dict[str, Any]]:
    """Score the samples. Returns, per metric, its mean and how many samples scored (a sample the
    judge could not score is NaN in RAGAS and is left out, never counted as zero)."""
    if not samples:
        return {}
    result = evaluate(
        EvaluationDataset(samples=list(samples)),
        metrics=ragas_metrics(),
        llm=ProviderRagasLLM(llm, routes),
        embeddings=ProviderRagasEmbeddings(embedder),
        raise_exceptions=False,
        show_progress=False,
    )
    frame = cast(Any, result).to_pandas()
    scored: dict[str, dict[str, Any]] = {}
    for column, name in METRIC_NAMES.items():
        if column not in frame.columns:
            continue
        series = frame[column].dropna()
        scored[name] = {
            "value": float(series.mean()) if len(series) else None,
            "n": len(series),
            "failed": int(len(frame) - len(series)),
        }
    return scored
