"""The offline evaluation end to end on a micro dataset, against real PostgreSQL: it runs clean,
it is deterministic, it cleans up after itself, and its regression gate catches regressions."""

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from covenantiq.rag.config import EvidenceConfig, RagConfig, RetrievalConfig
from eval import pipeline
from eval.baseline import baseline_from_report, compare, load_tolerances
from eval.dataset import Dataset, load_dataset
from eval.report import write_report
from eval.runner import make_report, offline_providers, run_pipeline
from eval.scripts import run_offline
from tests.integration.vendor.stack import UowFactory

pytestmark = pytest.mark.asyncio


def micro() -> Dataset:
    """Eight questions over two documents, two contracts, one vendor: about 13 seconds."""
    return load_dataset().subset(chat=8, findings=2, vendors=1)


async def report_for(uow_factory: UowFactory, rag: RagConfig | None = None) -> dict[str, Any]:
    dataset = micro()
    providers = offline_providers()
    run = await run_pipeline(dataset, uow_factory, providers, rag=rag)
    return make_report("offline", dataset, run, providers)


def values(report: dict[str, Any]) -> dict[str, float | None]:
    return {name: m["value"] for name, m in report["metrics"].items()}


def verdict(baseline_report: dict[str, Any], report: dict[str, Any]):  # type: ignore[no-untyped-def]
    return compare(baseline_from_report(baseline_report), report, load_tolerances())


class TestARun:
    async def test_it_runs_clean_and_reports_every_metric(self, uow_factory: UowFactory) -> None:
        report = await report_for(uow_factory)

        assert report["errors"] == []
        expected = {
            "hit_at_1",
            "hit_at_3",
            "hit_at_5",
            "mrr",
            "context_precision_nonllm",
            "context_recall_nonllm",
            "citation_correctness",
            "citation_validity_rate",
            "unsupported_claim_rate",
            "abstention_accuracy",
            "false_abstention_rate",
            "false_answer_rate",
            "gate_abstention_on_unanswerable",
            "clause_extraction_accuracy",
            "risk_classification_precision",
            "risk_classification_recall",
            "finding_status_accuracy",
            "not_expected_violation_rate",
            "vendor_scoring_determinism",
            "vendor_signal_agreement",
            "vendor_dimension_order",
        }
        assert expected <= set(report["metrics"])
        assert report["metrics"]["hit_at_5"]["n"] == 8
        assert report["metrics"]["vendor_scoring_determinism"]["value"] == 1.0

    async def test_the_report_says_what_it_was_run_on_and_with(
        self, uow_factory: UowFactory
    ) -> None:
        report = await report_for(uow_factory)
        env = report["environment"]
        assert report["run"] == "offline" and report["dataset"]["version"] == "1.0.0"
        assert len(report["dataset"]["manifest_sha256"]) == 64
        assert env["ruleset_version"] == "contract_baseline_v1@1.0.0"
        assert env["scoring_config_version"] == "vendor_scoring_v1@1.0.0"
        assert env["models"]["all"] == "scripted-fake" and env["git_sha"]
        assert {"chat_answer.v1", "rule_assessment.v1", "signal_extraction.v1"} <= set(
            env["prompts"]
        )
        assert report["per_example"]["chat"] and report["per_example"]["findings"]

    async def test_it_writes_json_and_a_markdown_summary(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        report = await report_for(uow_factory)
        json_path, md_path = write_report(report, tmp_path)
        assert (
            json.loads(json_path.read_text(encoding="utf-8"))["metrics"].keys()
            == report["metrics"].keys()
        )
        summary = md_path.read_text(encoding="utf-8")
        assert "| hit_at_5 |" in summary and "95% CI" in summary and "lower is better" in summary

    async def test_it_is_deterministic(self, uow_factory: UowFactory) -> None:
        first, second = await report_for(uow_factory), await report_for(uow_factory)
        assert values(first) == values(second)

    async def test_every_interval_brackets_its_value(self, uow_factory: UowFactory) -> None:
        report = await report_for(uow_factory)
        for name, metric in report["metrics"].items():
            low, high = metric["ci95"]
            if metric["value"] is not None:
                assert low <= metric["value"] <= high, name

    async def test_it_removes_everything_it_created(self, uow_factory: UowFactory) -> None:
        await report_for(uow_factory)
        async with uow_factory() as uow:
            for table in ("users", "documents", "document_chunks", "analysis_jobs"):
                query = text(f"SELECT count(*) FROM {table}")  # noqa: S608  (fixed table names)
                count = (await uow.session.execute(query)).scalar_one()
                assert count == 0, table


class TestIngestion:
    async def test_every_document_is_chunked_embedded_and_ready(
        self, uow_factory: UowFactory, tmp_path: Path
    ) -> None:
        dataset = load_dataset()
        providers = offline_providers()
        world = await pipeline.ingest(
            dataset,
            uow_factory,
            providers.embedder,
            providers.reranker,
            tmp_path,
            RagConfig(),
            only={"acme_saas.txt", "globex_saas.txt"},
        )
        try:
            async with uow_factory() as uow:
                rows = (
                    await uow.session.execute(
                        text(
                            "SELECT d.title, d.status, d.contract_type, count(c.id), "
                            "count(c.embedding) "
                            "FROM documents d JOIN document_chunks c ON c.document_id = d.id "
                            "GROUP BY d.title, d.status, d.contract_type ORDER BY d.title"
                        )
                    )
                ).all()
            assert [(r[0], r[1], r[2]) for r in rows] == [
                ("acme_saas.txt", "ready", "saas"),
                ("globex_saas.txt", "ready", "saas"),
            ]
            assert all(r[3] > 5 and r[3] == r[4] for r in rows)  # every chunk has an embedding
        finally:
            await pipeline.cleanup(world)


class TestTheRegressionGate:
    async def test_a_second_healthy_run_passes_against_the_first(
        self, uow_factory: UowFactory
    ) -> None:
        baseline = await report_for(uow_factory)
        result = verdict(baseline, await report_for(uow_factory))
        assert result.passed, result.render()
        assert all(r.status in ("ok", "improved") for r in result.rows)

    async def test_returning_too_few_passages_is_caught(self, uow_factory: UowFactory) -> None:
        baseline = await report_for(uow_factory)
        degraded = await report_for(uow_factory, RagConfig(retrieval=RetrievalConfig(k_final=1)))
        result = verdict(baseline, degraded)
        assert not result.passed
        regressed = {r.name for r in result.rows if r.status == "regressed"}
        assert {"hit_at_3", "hit_at_5", "mrr"} <= regressed, result.render()

    async def test_an_evidence_gate_set_too_strict_is_caught(self, uow_factory: UowFactory) -> None:
        baseline = await report_for(uow_factory)
        strict = RagConfig(
            evidence=EvidenceConfig(
                rerank_strong=1.0, rerank_moderate=1.0, rerank_single_strong=1.0
            )
        )
        result = verdict(baseline, await report_for(uow_factory, strict))
        assert not result.passed
        regressed = {r.name for r in result.rows if r.status == "regressed"}
        assert {"abstention_accuracy", "false_abstention_rate"} <= regressed, result.render()

    async def test_raising_the_rrf_constant_is_not_caught_because_it_changes_nothing_here(
        self, uow_factory: UowFactory
    ) -> None:
        """The spec's example regression (RRF k to 1,000) does not move any offline metric: the
        reranker reorders the fused candidates, so the constant only affects which ones are in the
        set. Recorded here so nobody relies on this gate to catch it."""
        baseline = await report_for(uow_factory)
        changed = await report_for(uow_factory, RagConfig(retrieval=RetrievalConfig(rrf_k=1000)))
        assert values(changed) == values(baseline)


class TestTheScript:
    async def test_it_runs_end_to_end_and_writes_a_baseline_the_gate_accepts(
        self, database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            run_offline,
            "load_dataset",
            lambda version=None: micro(),  # the micro dataset
        )
        args = argparse.Namespace(
            database_url=database_url,
            dataset="v1.0.0",
            out=str(tmp_path / "reports"),
            micro=False,
            write_baseline=str(tmp_path / "baseline.json"),
            note="a test",
        )
        assert await run_offline.main_async(args) == 0

        baseline = json.loads((tmp_path / "baseline.json").read_text(encoding="utf-8"))
        report_path = next((tmp_path / "reports").glob("*_offline.json"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert baseline["note"] == "a test"
        assert compare(baseline, report, load_tolerances()).passed
