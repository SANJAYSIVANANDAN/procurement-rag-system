"""The full evaluation: real providers and RAGAS's LLM-judged metrics (spec 28.5).

    CIQ_OFFLINE=0 uv run python -m eval.scripts.run_full [--database-url URL]

Needs `uv sync --group eval`, provider keys in the environment (the same ones the application uses:
the model, embeddings and, if configured, the reranker) and a migrated PostgreSQL. It costs money:
every chat question, every contract (25 rules) and every RAGAS judgement is a model call, so it
asks before spending anything unless `--yes` is given. It refuses to run with fake providers,
because a "full" report made with them would be a fabricated one (spec 28.1).

The report is written to `eval/reports/`. This milestone's author had no provider keys, so this
script has been written and its pieces tested with fakes, but never run for real.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from covenantiq.ai.embeddings.factory import build_embedding_provider
from covenantiq.ai.llm.factory import build_llm
from covenantiq.ai.rerank.factory import build_reranker
from covenantiq.core.config import Settings
from covenantiq.db.session import create_session_factory
from covenantiq.db.uow import UnitOfWork
from eval.dataset import DEFAULT_VERSION, load_dataset
from eval.report import build_report, markdown_summary, write_report
from eval.runner import Providers, compute_metrics, run_pipeline
from eval.scripts.run_offline import check_schema, database_url

# Initial targets of spec 28.7: targets to be validated, not results.
TARGETS: dict[str, tuple[str, float]] = {
    "ragas_faithfulness": (">=", 0.85),
    "ragas_answer_relevancy": (">=", 0.80),
    "ragas_context_recall_llm": (">=", 0.80),
    "ragas_context_precision_llm": (">=", 0.70),
    "citation_correctness": (">=", 0.90),
    "unsupported_claim_rate": ("<=", 0.10),
    "abstention_accuracy": (">=", 0.85),
    "false_answer_rate": ("<=", 0.10),
    "clause_extraction_accuracy": (">=", 0.85),
    "risk_classification_precision": (">=", 0.75),
    "risk_classification_recall": (">=", 0.70),
    "vendor_scoring_determinism": (">=", 1.0),
}


class NotARealRun(RuntimeError):
    pass


def require_real_providers(settings: Settings) -> None:
    """Refuse fakes: a full report made with them would pass for a measurement and be nothing."""
    fakes = [
        name
        for name, value in (
            ("CIQ_LLM_PROVIDER", settings.llm_provider),
            ("CIQ_EMBEDDING_PROVIDER", settings.embedding_provider),
        )
        if value == "fake"
    ]
    if settings.offline:
        fakes.append("CIQ_OFFLINE")
    if fakes:
        raise NotARealRun(
            "the full evaluation needs real providers; these are fake or offline: "
            + ", ".join(fakes)
        )


def target_lines(metrics: dict[str, dict[str, object]]) -> list[str]:
    lines = []
    for name, (relation, target) in TARGETS.items():
        value = metrics.get(name, {}).get("value")
        if value is None:
            lines.append(f"{name}: not measured (target {relation} {target})")
            continue
        met = value >= target if relation == ">=" else value <= target  # type: ignore[operator]
        lines.append(
            f"{name}: {value:.3f} (target {relation} {target}) {'met' if met else 'NOT MET'}"
        )
    return lines


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    require_real_providers(settings)
    url = database_url(args.database_url)
    await check_schema(url)
    dataset = load_dataset(args.dataset)
    calls = len(dataset.chat) * 3 + len(dataset.findings) * 8 + len(dataset.chat) * 6
    if not args.yes:
        reply = await asyncio.to_thread(
            input,
            f"This makes roughly {calls} paid model calls (more with retries). Continue? [y/N] ",
        )
        if reply.strip().lower() not in ("y", "yes"):
            print("aborted: nothing was spent")
            return 1

    llm = build_llm(settings)
    embedder = build_embedding_provider(settings)
    reranker = build_reranker(settings)
    providers = Providers(
        embedder=embedder,
        reranker=reranker,
        llm=llm.provider,
        routes=llm.routes,
        models={
            "answer": llm.routes.model_for("answer"),
            "rule_assessment": llm.routes.model_for("rule_assessment"),
            "judge": llm.routes.model_for("eval_judge"),
            "embedding": settings.embedding_model,
            "rerank": settings.rerank_model if reranker else "none",
        },
    )
    engine = create_async_engine(url, poolclass=NullPool)
    factory = create_session_factory(engine)
    try:
        run = await run_pipeline(
            dataset,
            lambda: UnitOfWork(factory),
            providers,
            on_progress=lambda message: print(message, file=sys.stderr),
        )
    finally:
        await engine.dispose()

    metrics, per_example = compute_metrics(run)
    report = build_report(
        "full", dataset, metrics, per_example, providers.models, errors=run.errors
    )

    from eval.ragas_adapter import build_samples, run_ragas  # needs the `eval` dependency group

    samples = build_samples(run.chat)
    print(f"judging {len(samples)} answers with RAGAS", file=sys.stderr)
    for name, scored in run_ragas(samples, llm.provider, llm.routes, embedder).items():
        report["metrics"][name] = {
            "name": name,
            "value": scored["value"],
            "n": scored["n"],
            "ci95": [None, None],
            "higher_is_better": True,
            "failed": scored["failed"],
        }
    report["notes"].append(
        "vendor extraction stability (three live assessments) is not part of this runner yet"
    )
    json_path, _ = write_report(report, Path(args.out))
    print(markdown_summary(report))
    print("\n".join(target_lines(report["metrics"])))
    print(f"report: {json_path}", file=sys.stderr)
    return 2 if report["errors"] else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--dataset", default=DEFAULT_VERSION)
    parser.add_argument("--out", default="eval/reports")
    parser.add_argument("--yes", action="store_true", help="do not ask before spending")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        code = asyncio.run(main_async(args))
    except NotARealRun as exc:
        print(f"refused: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
