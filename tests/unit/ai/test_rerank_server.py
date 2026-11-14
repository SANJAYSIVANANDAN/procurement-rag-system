import asyncio
import os
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from covenantiq.ai.rerank.server import (
    MAX_DOCUMENT_CHARS,
    MAX_QUERY_CHARS,
    DeadlineExceeded,
    create_app,
    score_in_batches,
)


class WordCountScorer:
    """Scores a document by how many query words it contains (deterministic, instant)."""

    name = "test-scorer"

    def __init__(self) -> None:
        self.calls = 0

    def score(
        self, query: str, documents: Sequence[str], deadline: float | None = None
    ) -> list[float]:
        self.calls += 1
        words = set(query.lower().split())
        return [float(len(words & set(doc.lower().split()))) for doc in documents]


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://reranker.test"
        ) as client:
            yield client


async def test_ranks_documents_best_first_and_honours_top_k() -> None:
    app = create_app(WordCountScorer)
    async with running(app) as client:
        response = await client.post(
            "/v1/rerank",
            json={
                "query": "supplier liability cap",
                "documents": [
                    "kitchen rota",
                    "supplier liability cap applies",
                    "supplier invoices",
                ],
                "top_k": 2,
            },
        )
    body = response.json()
    assert response.status_code == 200
    assert body["model"] == "test-scorer"
    assert [hit["index"] for hit in body["results"]] == [1, 2]
    assert [hit["score"] for hit in body["results"]] == [3.0, 1.0]  # raw scores, best first
    assert isinstance(body["elapsed_ms"], int)


async def test_ties_keep_the_original_order() -> None:
    async with running(create_app(WordCountScorer)) as client:
        response = await client.post(
            "/v1/rerank", json={"query": "zzz", "documents": ["a", "b", "c"]}
        )
    assert [hit["index"] for hit in response.json()["results"]] == [0, 1, 2]


async def test_the_response_never_contains_the_text_it_scored() -> None:
    async with running(create_app(WordCountScorer)) as client:
        response = await client.post(
            "/v1/rerank",
            json={"query": "secret question", "documents": ["confidential clause text"]},
        )
    assert "confidential" not in response.text
    assert "secret" not in response.text


async def test_readiness_follows_model_loading() -> None:
    app = create_app(WordCountScorer)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://reranker.test") as client:
        assert (await client.get("/health/live")).status_code == 200
    async with running(app) as client:
        ready = await client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ok", "model": "test-scorer"}


class TestLimits:
    async def test_empty_document_list_is_rejected(self) -> None:
        async with running(create_app(WordCountScorer)) as client:
            response = await client.post("/v1/rerank", json={"query": "q", "documents": []})
        assert response.status_code == 422

    async def test_too_many_documents_are_rejected(self) -> None:
        async with running(create_app(WordCountScorer, max_documents=3)) as client:
            response = await client.post(
                "/v1/rerank", json={"query": "q", "documents": ["a", "b", "c", "d"]}
            )
        assert response.status_code == 413

    @pytest.mark.parametrize(
        "payload",
        [
            {"query": "", "documents": ["a"]},
            {"query": "x" * (MAX_QUERY_CHARS + 1), "documents": ["a"]},
            {"query": "q", "documents": ["x" * (MAX_DOCUMENT_CHARS + 1)]},
            {"query": "q", "documents": ["a"], "top_k": 0},
            {"documents": ["a"]},
        ],
    )
    async def test_malformed_requests_are_422(self, payload: dict[str, object]) -> None:
        async with running(create_app(WordCountScorer)) as client:
            assert (await client.post("/v1/rerank", json=payload)).status_code == 422


class SlowScorer:
    """Blocks inside `score` until released, and records how many calls overlap."""

    name = "slow"

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def score(
        self, query: str, documents: Sequence[str], deadline: float | None = None
    ) -> list[float]:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.started.set()
        self.release.wait(timeout=10)
        with self._lock:
            self.active -= 1
        return [1.0] * len(documents)


async def post(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/v1/rerank", json={"query": "q", "documents": ["a"]})


class TestUnderLoad:
    async def test_scoring_does_not_block_the_event_loop(self) -> None:
        scorer = SlowScorer()
        async with running(create_app(lambda: scorer)) as client:
            job = asyncio.create_task(post(client))
            await asyncio.to_thread(scorer.started.wait, 5)

            started = time.monotonic()
            live = await client.get("/health/live")  # answered while the model is busy
            assert live.status_code == 200
            assert time.monotonic() - started < 0.5

            scorer.release.set()
            assert (await job).status_code == 200

    async def test_requests_are_scored_one_at_a_time(self) -> None:
        scorer = SlowScorer()
        async with running(create_app(lambda: scorer, max_pending=5)) as client:
            jobs = [asyncio.create_task(post(client)) for _ in range(3)]
            await asyncio.to_thread(scorer.started.wait, 5)
            await asyncio.sleep(0.2)
            scorer.release.set()
            responses = await asyncio.gather(*jobs)
        assert [r.status_code for r in responses] == [200, 200, 200]
        assert scorer.max_active == 1  # the model never ran two jobs at once

    async def test_overload_is_answered_503_at_once_instead_of_queueing(self) -> None:
        scorer = SlowScorer()
        async with running(create_app(lambda: scorer, max_pending=2)) as client:
            busy = [asyncio.create_task(post(client)) for _ in range(2)]
            await asyncio.to_thread(scorer.started.wait, 5)
            await asyncio.sleep(0.2)

            started = time.monotonic()
            shed = await post(client)
            assert time.monotonic() - started < 0.5
            assert shed.status_code == 503
            assert shed.headers["retry-after"] == "1"

            scorer.release.set()
            assert [r.status_code for r in await asyncio.gather(*busy)] == [200, 200]
            assert (await post(client)).status_code == 200  # and it recovers


class TestScoreInBatches:
    def predict_by_length(self, calls: list[int] | None = None):  # type: ignore[no-untyped-def]
        def predict(pairs: list[tuple[str, str]]) -> list[float]:
            if calls is not None:
                calls.append(len(pairs))
            return [float(len(doc)) for _, doc in pairs]

        return predict

    def test_scores_come_back_in_the_original_order(self) -> None:
        docs = ["ccc", "a", "bbbbb", "dd"]
        scores = score_in_batches(self.predict_by_length(), "q", docs, batch_size=2, deadline=None)
        assert scores == [3.0, 1.0, 5.0, 2.0]

    def test_documents_are_scored_in_batches_shortest_first(self) -> None:
        sizes: list[int] = []
        seen: list[str] = []

        def predict(pairs: list[tuple[str, str]]) -> list[float]:
            sizes.append(len(pairs))
            seen.extend(doc for _, doc in pairs)
            return [0.0] * len(pairs)

        score_in_batches(
            predict, "q", ["xxxx", "x", "xxx", "xx", "xxxxx"], batch_size=2, deadline=None
        )
        assert sizes == [2, 2, 1]
        assert seen == ["x", "xx", "xxx", "xxxx", "xxxxx"]  # least padding

    def test_it_stops_between_batches_when_the_deadline_passes(self) -> None:
        now = [0.0]
        scored: list[int] = []

        def predict(pairs: list[tuple[str, str]]) -> list[float]:
            scored.append(len(pairs))
            now[0] += 1.0  # each batch takes a second
            return [0.0] * len(pairs)

        with pytest.raises(DeadlineExceeded):
            score_in_batches(
                predict, "q", ["a"] * 10, batch_size=2, deadline=2.5, clock=lambda: now[0]
            )
        # after two 1 s batches the clock reads 2.0; another would end at 3.0, past 2.5: not started
        assert scored == [2, 2]

    def test_it_does_not_start_a_batch_it_expects_to_overrun(self) -> None:
        now = [0.0]
        scored: list[int] = []

        def predict(pairs: list[tuple[str, str]]) -> list[float]:
            scored.append(len(pairs))
            now[0] += 2.5  # a slow machine: each batch takes 2.5 s
            return [0.0] * len(pairs)

        with pytest.raises(DeadlineExceeded):
            score_in_batches(
                predict, "q", ["a"] * 8, batch_size=4, deadline=2.7, clock=lambda: now[0]
            )
        assert scored == [4]  # the second batch would have ended at 5.0, so the CPU is freed at 2.5

    def test_a_deadline_that_has_already_passed_scores_nothing(self) -> None:
        scored: list[int] = []
        with pytest.raises(DeadlineExceeded):
            score_in_batches(
                self.predict_by_length(scored),
                "q",
                ["a"],
                batch_size=1,
                deadline=-1.0,
                clock=lambda: 0.0,
            )
        assert scored == []

    def test_no_deadline_scores_everything(self) -> None:
        assert (
            len(
                score_in_batches(
                    self.predict_by_length(), "q", ["a"] * 9, batch_size=4, deadline=None
                )
            )
            == 9
        )


class DeadlineScorer:
    name = "deadline"

    def score(
        self, query: str, documents: Sequence[str], deadline: float | None = None
    ) -> list[float]:
        # behaves like a model too slow for the caller's budget
        raise DeadlineExceeded


class TestDeadline:
    async def test_a_scorer_that_runs_out_of_time_gives_504(self) -> None:
        async with running(create_app(DeadlineScorer)) as client:
            response = await client.post(
                "/v1/rerank", json={"query": "q", "documents": ["a"], "deadline_ms": 1000}
            )
        assert response.status_code == 504

    async def test_the_deadline_reaches_the_scorer_and_counts_from_arrival(self) -> None:
        seen: list[float | None] = []

        class Recording:
            name = "recording"

            def score(
                self, query: str, documents: Sequence[str], deadline: float | None = None
            ) -> list[float]:
                seen.append(deadline)
                return [1.0] * len(documents)

        async with running(create_app(Recording)) as client:
            before = time.monotonic()
            await client.post(
                "/v1/rerank", json={"query": "q", "documents": ["a"], "deadline_ms": 2000}
            )
            await client.post("/v1/rerank", json={"query": "q", "documents": ["a"]})
        assert seen[0] is not None
        assert before + 1.9 < seen[0] < before + 2.5
        assert seen[1] is None

    async def test_time_spent_waiting_for_the_model_counts_against_the_budget(self) -> None:
        class Honouring(SlowScorer):
            scored_for: list[str] = []  # noqa: RUF012

            def score(
                self, query: str, documents: Sequence[str], deadline: float | None = None
            ) -> list[float]:
                if deadline is not None and time.monotonic() >= deadline:
                    raise DeadlineExceeded
                self.scored_for.append(query)
                return super().score(query, documents, deadline)

        scorer = Honouring()
        async with running(create_app(lambda: scorer, max_pending=5)) as client:
            first = asyncio.create_task(post(client))
            await asyncio.to_thread(scorer.started.wait, 5)
            second = asyncio.create_task(
                client.post(
                    "/v1/rerank",
                    json={"query": "late", "documents": ["a"], "deadline_ms": 50},
                )
            )
            await asyncio.sleep(0.3)  # the second request waits well past its 50 ms
            scorer.release.set()
            assert (await first).status_code == 200
            late = await second
        assert late.status_code == 504
        assert "late" not in scorer.scored_for  # its turn came after the deadline: not scored

    @pytest.mark.parametrize("deadline", [0, -5, 600_001])
    async def test_nonsense_deadlines_are_422(self, deadline: int) -> None:
        async with running(create_app(WordCountScorer)) as client:
            response = await client.post(
                "/v1/rerank", json={"query": "q", "documents": ["a"], "deadline_ms": deadline}
            )
        assert response.status_code == 422


def test_the_api_and_workers_never_import_the_model_stack(tmp_path: Path) -> None:
    """The point of the separate process: loading the application must not load PyTorch or
    sentence-transformers. Only `covenantiq rerank-serve` does.

    Runs in a clean directory with its own settings: importing the API builds the app, which reads
    `.env` from the working directory, and this test must not depend on a developer's file."""
    code = """
import sys
import covenantiq.api.main, covenantiq.workers.runtime, covenantiq.ai.rerank
import covenantiq.ai.rerank.factory, covenantiq.ai.rerank.remote
heavy = ('torch', 'sentence_transformers', 'transformers')
print('LOADED:' + ','.join(m for m in heavy if m in sys.modules))
"""
    inherited = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("CIQ_")
        and name not in {"ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "OPENAI_API_KEY"}
    }
    environment = {
        **inherited,
        "CIQ_OFFLINE": "1",
        "CIQ_DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
        "CIQ_CHECKPOINT_DB_URL": "postgresql://u:p@localhost/db",
        "CIQ_REDIS_URL": "redis://localhost:6379/0",
        "CIQ_CELERY_RESULT_BACKEND": "redis://localhost:6379/1",
        "CIQ_RABBITMQ_URL": "amqp://u:p@localhost:5672/v",
        "CIQ_JWT_SECRET": "x" * 40,
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=environment,
    )
    assert result.returncode == 0, result.stderr[-500:]
    assert result.stdout.strip().splitlines()[-1] == "LOADED:"
