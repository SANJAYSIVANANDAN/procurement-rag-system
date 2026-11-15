"""Running an evaluation and turning its runs into metrics (spec 28.5)."""

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from covenantiq.ai.embeddings.base import EmbeddingProvider
from covenantiq.ai.embeddings.fake import HashEmbeddingProvider
from covenantiq.ai.llm.base import LLMProvider
from covenantiq.ai.llm.fake import FakeLLMProvider
from covenantiq.ai.llm.routes import RouteTable
from covenantiq.ai.rerank.base import Reranker
from covenantiq.ai.rerank.fake import LexicalOverlapReranker
from covenantiq.ai.schemas import ObligationExtraction
from covenantiq.rag.config import RagConfig
from eval import pipeline
from eval.dataset import ChatExample, Dataset, ExpectedFinding
from eval.metrics import answers, findings, retrieval, stats, vendor
from eval.metrics.answers import AbstentionOutcome
from eval.metrics.findings import ProducedFinding
from eval.metrics.stats import MetricValue, Ratio
from eval.pipeline import ChatRun, FindingsRun, UowFactory, VendorRun
from eval.report import build_report
from eval.scripted import oracle_assessments, scripted_answer

HIT_KS = (1, 3, 5)


@dataclass
class Providers:
    embedder: EmbeddingProvider
    reranker: Reranker | None
    llm: LLMProvider
    routes: RouteTable
    models: dict[str, str]
    scripts: "OfflineScripts | None" = None


class OfflineScripts:
    """Points the scripted model at the example being run."""

    def __init__(self, llm: FakeLLMProvider) -> None:
        self.example: ChatExample | None = None
        self.expected: dict[str, ExpectedFinding] = {}
        llm.script(
            "classify",
            lambda r: {
                "intent": "document_qa",
                "is_follow_up": bool(self.example and self.example.conversation_history),
            },
        )
        llm.script(
            "rewrite",
            lambda r: {
                "query": (self.example.standalone_question or self.example.question)
                if self.example
                else ""
            },
        )
        llm.script("answer", self._answer)
        llm.script(
            "rule_assessment", lambda r: oracle_assessments(r.messages[0].content, self.expected)
        )
        llm.script("signal_extraction", lambda r: ObligationExtraction(obligations=[]))

    def _answer(self, request: Any) -> str:
        if self.example is None:
            raise RuntimeError("no example is being run")
        return scripted_answer(request.messages[-1].content, self.example)


def offline_providers() -> Providers:
    """Hash embeddings, a lexical reranker and a scripted model: deterministic and free."""
    llm = FakeLLMProvider()
    return Providers(
        embedder=HashEmbeddingProvider(),
        reranker=LexicalOverlapReranker(),
        llm=llm,
        routes=RouteTable({}, "scripted-fake"),
        models={"all": "scripted-fake", "embedding": "fake-hash", "rerank": "fake-lexical"},
        scripts=OfflineScripts(llm),
    )


@dataclass
class Evaluation:
    chat: list[tuple[ChatExample, ChatRun]] = field(default_factory=list)
    findings: list[tuple[Any, FindingsRun]] = field(default_factory=list)
    vendors: list[tuple[Any, VendorRun]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def run_pipeline(
    dataset: Dataset,
    uow_factory: UowFactory,
    providers: Providers,
    *,
    rag: RagConfig | None = None,
    storage_root: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> Evaluation:
    rag = rag or RagConfig()
    say = on_progress or (lambda _msg: None)
    result = Evaluation()
    with tempfile.TemporaryDirectory(prefix="ciq-eval-") as scratch:
        world = await pipeline.ingest(
            dataset,
            uow_factory,
            providers.embedder,
            providers.reranker,
            storage_root or Path(scratch),
            rag,
            only=dataset.documents_used(),
        )
        try:
            say(f"ingested {len(world.documents)} documents")
            chat = pipeline.ChatEvaluator(world, providers.llm, providers.routes)
            for example in dataset.chat:
                if providers.scripts:
                    providers.scripts.example = example
                run = await chat.run(example)
                if run.error:
                    result.errors.append(f"chat {example.id}: {run.error}")
                result.chat.append((example, run))
            say(f"answered {len(result.chat)} questions")

            analyser = pipeline.FindingsEvaluator(world, providers.llm, providers.routes)
            by_document: dict[str, FindingsRun] = {}
            for item in dataset.findings:
                if providers.scripts:
                    providers.scripts.expected = {f.rule_id: f for f in item.expected_findings}
                run_f = await analyser.run(item)
                if run_f.error:
                    result.errors.append(f"findings {item.id}: {run_f.error}")
                by_document[item.document] = run_f
                result.findings.append((item, run_f))
            say(f"analysed {len(result.findings)} contracts")

            for case in dataset.vendors:
                if all(d in by_document for d in case.documents):
                    result.vendors.append(
                        (case, pipeline.evaluate_vendor(case, by_document, world))
                    )
            say(f"assessed {len(result.vendors)} vendors")
        finally:
            await pipeline.cleanup(world)
    return result


# ---- metrics ------------------------------------------------------------------------------------


def compute_metrics(run: Evaluation) -> tuple[list[MetricValue], dict[str, list[dict[str, Any]]]]:
    chat = [(e, r) for e, r in run.chat if not r.error]
    metrics: list[MetricValue] = []
    per_chat: list[dict[str, Any]] = []

    # retrieval: over questions that have a labelled passage
    answerable = [(e, r) for e, r in chat if e.relevant_passages]
    for k in HIT_KS:
        metrics.append(
            stats.summarise(
                f"hit_at_{k}",
                [
                    retrieval.hit_at_k(r.retrieved, [p.text for p in e.relevant_passages], k)
                    for e, r in answerable
                ],
            )
        )
    metrics.append(
        stats.summarise(
            "mrr",
            [
                retrieval.reciprocal_rank(r.retrieved, [p.text for p in e.relevant_passages])
                for e, r in answerable
            ],
        )
    )
    metrics.append(
        stats.summarise(
            "context_precision_nonllm",
            [
                retrieval.context_precision(r.retrieved, [p.text for p in e.relevant_passages])
                for e, r in answerable
            ],
        )
    )
    metrics.append(
        stats.summarise(
            "context_recall_nonllm",
            [
                retrieval.context_recall(r.retrieved, [p.text for p in e.relevant_passages])
                for e, r in answerable
            ],
        )
    )

    # answers
    metrics.append(
        stats.summarise(
            "citation_correctness",
            [answers.citation_correctness(r.citations, e.relevant_passages) for e, r in answerable],
        )
    )
    # The scripted model fabricates a claim for an unanswerable question (to test the safety net),
    # so the claim and marker metrics describe only the faithful reference answers.
    with_validation = [r for e, r in chat if e.answerable and r.validation]
    metrics.append(
        stats.summarise(
            "citation_validity_rate",
            [
                answers.citation_validity(v["markers"], v["invalid_markers"])
                for v in (r.validation for r in with_validation)
            ],
        )
    )
    metrics.append(
        stats.summarise(
            "unsupported_claim_rate",
            [
                answers.unsupported_claim_rate(
                    v["unsupported"], v["unmarked_claims"], v["factual_sentences"]
                )
                for v in (r.validation for r in with_validation)
            ],
            higher_is_better=False,
        )
    )
    outcomes = [AbstentionOutcome(e.answerable, r.abstained) for e, r in chat]
    metrics.append(stats.summarise("abstention_accuracy", answers.abstention_accuracy(outcomes)))
    metrics.append(
        stats.summarise(
            "false_abstention_rate", answers.false_abstention_rate(outcomes), higher_is_better=False
        )
    )
    metrics.append(
        stats.summarise(
            "false_answer_rate", answers.false_answer_rate(outcomes), higher_is_better=False
        )
    )
    metrics.append(
        stats.summarise(
            "gate_abstention_on_unanswerable",
            [(0.0 if r.answer_model_called else 1.0, 1.0) for e, r in chat if not e.answerable],
        )
    )
    for example, r in run.chat:
        hit5 = retrieval.hit_at_k(r.retrieved, [p.text for p in example.relevant_passages], 5)
        per_chat.append(
            {
                "id": example.id,
                "answerable": example.answerable,
                "abstained": r.abstained,
                "grounding": r.grounding,
                "hit_at_5": hit5[0] if hit5[1] else None,
                "citations": len(r.citations),
                "answer_model_called": r.answer_model_called,
                "error": r.error,
            }
        )

    # contract findings
    produced_pairs: dict[str, list[Ratio]] = {
        k: [] for k in ("clause", "precision", "recall", "status", "violations")
    }
    per_findings: list[dict[str, Any]] = []
    for item, fr in run.findings:
        if fr.error:
            continue
        produced = [
            ProducedFinding(d.rule_id, d.clause_status.value, [e.text for e in d.evidence])
            for d in fr.drafts
            if d.details.get("kind") == "rule"
        ]
        produced_pairs["clause"].append(
            findings.clause_extraction_accuracy(item.expected_findings, produced)
        )
        produced_pairs["precision"].append(
            findings.risk_precision(item.expected_findings, produced)
        )
        produced_pairs["recall"].append(findings.risk_recall(item.expected_findings, produced))
        produced_pairs["status"].append(findings.status_accuracy(item.expected_findings, produced))
        produced_pairs["violations"].append(
            findings.not_expected_violations(item.not_expected, produced)
        )
        got = {p.rule_id: p.status for p in produced}
        per_findings.append(
            {
                "id": item.id,
                "document": item.document,
                "status_accuracy": produced_pairs["status"][-1][0]
                / max(1.0, produced_pairs["status"][-1][1]),
                "wrong": {
                    e.rule_id: [e.status, got.get(e.rule_id)]
                    for e in item.expected_findings
                    if got.get(e.rule_id) != e.status
                },
            }
        )
    metrics += [
        stats.summarise("clause_extraction_accuracy", produced_pairs["clause"]),
        stats.summarise("risk_classification_precision", produced_pairs["precision"]),
        stats.summarise("risk_classification_recall", produced_pairs["recall"]),
        stats.summarise("finding_status_accuracy", produced_pairs["status"]),
        stats.summarise(
            "not_expected_violation_rate", produced_pairs["violations"], higher_is_better=False
        ),
    ]

    # vendors
    scores = {vr.vendor: vr.dimension_scores for _, vr in run.vendors}
    per_vendor: list[dict[str, Any]] = []
    agreement, order, determinism = [], [], []
    for case, vr in run.vendors:
        agreement.append(vendor.signal_agreement(case.expected_signals, vr.signals))
        order.append(vendor.dimension_order(case.expected_dimension_order, vr.vendor, scores))
        determinism.append((1.0 if vr.deterministic else 0.0, 1.0))
        per_vendor.append(
            {
                "id": case.id,
                "vendor": vr.vendor,
                "disagreements": {
                    s: [e, vr.signals.get(s)]
                    for s, e in case.expected_signals.items()
                    if vr.signals.get(s) != e
                },
            }
        )
    metrics += [
        stats.summarise("vendor_scoring_determinism", determinism),
        stats.summarise("vendor_signal_agreement", agreement),
        stats.summarise("vendor_dimension_order", order),
    ]
    return metrics, {"chat": per_chat, "findings": per_findings, "vendors": per_vendor}


def make_report(
    kind: str, dataset: Dataset, run: Evaluation, providers: Providers
) -> dict[str, Any]:
    metrics, per_example = compute_metrics(run)
    return build_report(kind, dataset, metrics, per_example, providers.models, errors=run.errors)
