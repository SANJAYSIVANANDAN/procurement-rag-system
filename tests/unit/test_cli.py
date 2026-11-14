import pytest
from typer.testing import CliRunner

from covenantiq import __version__
from covenantiq.cli import app

runner = CliRunner()


def test_help_runs() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "check-config" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_migrate_upgrades_to_head_then_creates_the_checkpoint_tables(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr("covenantiq.cli.command.upgrade", lambda _config, rev: calls.append(rev))
    monkeypatch.setattr(
        "covenantiq.cli.setup_checkpointer_sync", lambda url: calls.append(f"checkpoint:{url}")
    )
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0
    assert calls == ["head", f"checkpoint:{valid_env['CIQ_CHECKPOINT_DB_URL']}"]
    assert "checkpoint tables are ready" in result.output


def test_check_config_ok(valid_env: dict[str, str]) -> None:
    result = runner.invoke(app, ["check-config"])
    assert result.exit_code == 0
    assert "configuration ok" in result.output


def test_check_config_reports_problems_without_leaking_values(
    valid_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIQ_JWT_SECRET", "short-secret-value")
    result = runner.invoke(app, ["check-config"])
    assert result.exit_code == 1
    assert "jwt_secret" in result.output
    assert "short-secret-value" not in result.output


class TestOperationalCommands:
    def test_sweep_reports_what_it_did(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from covenantiq.services.sweep_service import SweepResult

        monkeypatch.setattr("covenantiq.cli.run_with_container", lambda _action: SweepResult(3))
        result = runner.invoke(app, ["sweep"])
        assert result.exit_code == 0
        assert "re-dispatched 3 job(s)" in result.output

    def test_sweep_says_when_another_sweeper_holds_the_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from covenantiq.services.sweep_service import SweepResult

        monkeypatch.setattr(
            "covenantiq.cli.run_with_container", lambda _a: SweepResult(0, skipped_by_lock=True)
        )
        assert "already running" in runner.invoke(app, ["sweep"]).output

    def test_check_embeddings_runs_against_the_offline_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name, value in {
            "CIQ_OFFLINE": "1",
            "CIQ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
            "CIQ_CHECKPOINT_DB_URL": "postgresql://u:p@localhost/db",
            "CIQ_REDIS_URL": "redis://localhost:6379/0",
            "CIQ_CELERY_RESULT_BACKEND": "redis://localhost:6379/1",
            "CIQ_RABBITMQ_URL": "amqp://u:p@localhost:5672/v",
            "CIQ_JWT_SECRET": "x" * 40,
        }.items():
            monkeypatch.setenv(name, value)
        result = runner.invoke(app, ["check-embeddings"])
        assert result.exit_code == 0, result.output
        assert "dimension=1024 (expected 1024)" in result.output
        assert "embeddings ok" in result.output

    def test_check_embeddings_reports_bad_settings_without_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CIQ_OFFLINE", "0")
        monkeypatch.setenv("CIQ_EMBEDDING_PROVIDER", "openai_compat")
        monkeypatch.delenv("CIQ_OPENAI_COMPAT_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        result = runner.invoke(app, ["check-embeddings"])
        assert result.exit_code == 1

    def configure(self, monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
        for name, value in {
            "CIQ_OFFLINE": "0",
            "CIQ_LLM_PROVIDER": "fake",
            "CIQ_EMBEDDING_PROVIDER": "fake",
            "CIQ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
            "CIQ_CHECKPOINT_DB_URL": "postgresql://u:p@localhost/db",
            "CIQ_REDIS_URL": "redis://localhost:6379/0",
            "CIQ_CELERY_RESULT_BACKEND": "redis://localhost:6379/1",
            "CIQ_RABBITMQ_URL": "amqp://u:p@localhost:5672/v",
            "CIQ_JWT_SECRET": "x" * 40,
            **extra,
        }.items():
            monkeypatch.setenv(name, value)

    def test_check_rerank_with_the_fake_reranker_ranks_the_liability_clause_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.configure(monkeypatch, CIQ_RERANK_PROVIDER="fake")
        result = runner.invoke(app, ["check-rerank"])
        assert result.exit_code == 0, result.output
        assert "rerank ok" in result.output
        assert "1. score=" in result.output

    def test_check_rerank_says_when_reranking_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.configure(monkeypatch, CIQ_RERANK_PROVIDER="none")
        result = runner.invoke(app, ["check-rerank"])
        assert result.exit_code == 0
        assert "switched off" in result.output

    def test_check_rerank_reports_an_unreachable_reranker_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.configure(
            monkeypatch,
            CIQ_RERANK_PROVIDER="cross_encoder",
            CIQ_RERANK_URL="http://127.0.0.1:9",  # nothing listens here
        )
        result = runner.invoke(app, ["check-rerank"])
        assert result.exit_code == 1
        assert "rerank failed" in result.output

    def test_check_rerank_with_cohere_and_no_key_fails_without_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("CIQ_COHERE_API_KEY", "COHERE_API_KEY", "CO_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        self.configure(monkeypatch, CIQ_RERANK_PROVIDER="cohere")
        result = runner.invoke(app, ["check-rerank"])
        assert result.exit_code == 1
        assert "COHERE_API_KEY is required" in result.output
        assert "Traceback" not in result.output

    def test_check_rerank_reports_a_failing_cohere_call_without_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.configure(
            monkeypatch,
            CIQ_RERANK_PROVIDER="cohere",
            COHERE_API_KEY="co-secret-value",
            CIQ_RERANK_TIMEOUT_SECONDS="0.5",
        )
        # no network in the unit tests: point the client at a closed local port
        monkeypatch.setattr("covenantiq.ai.rerank.cohere.BASE_URL", "http://127.0.0.1:9")
        result = runner.invoke(app, ["check-rerank"])
        assert result.exit_code == 1
        assert "rerank failed" in result.output
        assert "co-secret-value" not in result.output

    def test_rerank_serve_is_a_command_and_documents_what_it_does(self) -> None:
        result = runner.invoke(app, ["rerank-serve", "--help"])
        assert result.exit_code == 0
        assert "own process" in result.output

    def test_reembed_reports_counts_and_passes_the_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from covenantiq.services.reembed_service import ReembedResult

        monkeypatch.setattr("covenantiq.cli.run_with_container", lambda _a: ReembedResult(3, 1))
        result = runner.invoke(app, ["reembed", "--limit", "5"])
        assert result.exit_code == 0
        assert "queued 3 document(s) for re-embedding; skipped 1" in result.output

    def test_reembed_accepts_a_document_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from covenantiq.services.reembed_service import ReembedResult

        monkeypatch.setattr("covenantiq.cli.run_with_container", lambda _a: ReembedResult(1, 0))
        document = "0193a1d2-0000-7000-8000-000000000001"
        assert runner.invoke(app, ["reembed", "--document-id", document]).exit_code == 0

    def test_dlq_list_prints_each_dead_letter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from covenantiq.services.dlq_service import DeadLetter

        letters = [
            DeadLetter(
                "document_processing", "ingest.process_document", "t1", "job-1", "req-1", "rejected"
            )
        ]
        monkeypatch.setattr("covenantiq.cli.run_with_container", lambda _a: letters)
        result = runner.invoke(app, ["dlq", "list", "document_processing"])
        assert result.exit_code == 0
        assert "ingest.process_document" in result.output
        assert "target=job-1" in result.output
        assert "reason=rejected" in result.output

    def test_dlq_list_of_an_empty_queue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("covenantiq.cli.run_with_container", lambda _a: [])
        result = runner.invoke(app, ["dlq", "list", "document_processing"])
        assert "document_processing.dlq is empty" in result.output

    def test_dlq_replay_reports_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from covenantiq.services.dlq_service import ReplayResult

        monkeypatch.setattr("covenantiq.cli.run_with_container", lambda _a: ReplayResult(2, 1))
        result = runner.invoke(app, ["dlq", "replay", "document_processing"])
        assert result.exit_code == 0
        assert "replayed 2, skipped 1" in result.output

    def test_dlq_replay_accepts_a_job_id_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from covenantiq.services.dlq_service import ReplayResult

        monkeypatch.setattr("covenantiq.cli.run_with_container", lambda _a: ReplayResult(1, 0))
        job = "0193a1d2-0000-7000-8000-000000000001"
        result = runner.invoke(app, ["dlq", "replay", "document_processing", "--job-id", job])
        assert result.exit_code == 0

    def test_dlq_replay_rejects_a_malformed_job_id(self) -> None:
        result = runner.invoke(app, ["dlq", "replay", "document_processing", "--job-id", "nope"])
        assert result.exit_code != 0

    def test_unknown_queue_is_a_clean_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_action: object) -> None:
            raise ValueError("unknown queue 'x'; choose one of document_processing")

        monkeypatch.setattr("covenantiq.cli.run_with_container", boom)
        result = runner.invoke(app, ["dlq", "list", "x"])
        assert result.exit_code == 2
        assert "unknown queue" in result.output

    def test_commands_are_listed_in_help(self) -> None:
        output = runner.invoke(app, ["--help"]).output
        assert "sweep" in output and "dlq" in output
        assert "replay" in runner.invoke(app, ["dlq", "--help"]).output
