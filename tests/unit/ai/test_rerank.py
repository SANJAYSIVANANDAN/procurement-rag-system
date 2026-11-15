import json
import math

import httpx
import pytest
from pydantic import ValidationError

from covenantiq.ai.rerank import RerankRejected, RerankUnavailable, build_reranker, squash
from covenantiq.ai.rerank.fake import LexicalOverlapReranker
from covenantiq.ai.rerank.remote import (
    FAILURES_BEFORE_OPENING,
    MAX_DOCUMENT_CHARS,
    OPEN_FOR_SECONDS,
    RemoteReranker,
)
from covenantiq.core.config import ETTIN_RERANK_MODEL, VOYAGE_RERANK_MODEL, Settings


class TestSquash:
    def test_maps_raw_scores_into_zero_one(self) -> None:
        assert squash(0.0) == 0.5
        assert 0.99 < squash(8.0) < 1.0
        assert 0.0 < squash(-8.0) < 0.01

    def test_keeps_the_order(self) -> None:
        raw = [-3.0, -0.5, 0.0, 1.2, 4.9, 11.6]
        assert [squash(x) for x in raw] == sorted(squash(x) for x in raw)

    def test_never_overflows(self) -> None:
        assert squash(10_000.0) == 1.0
        assert squash(-10_000.0) == 0.0
        assert not math.isnan(squash(-10_000.0))


class TestLexicalOverlap:
    async def test_ranks_by_shared_query_words_and_honours_top_k(self) -> None:
        reranker = LexicalOverlapReranker()
        docs = ["kitchen cleaning rota", "the supplier liability cap", "payment within thirty days"]
        ranked = await reranker.rerank("supplier liability cap", docs, top_k=2)
        assert [index for index, _ in ranked] == [1, 0]
        assert ranked[0][1] == 1.0

    async def test_empty_query_scores_zero(self) -> None:
        assert await LexicalOverlapReranker().rerank("", ["a", "b"], top_k=5) == [
            (0, 0.0),
            (1, 0.0),
        ]


def remote(
    handler: httpx.MockTransport | None = None,
    *,
    clock: list[float] | None = None,
    max_documents: int = 100,
) -> RemoteReranker:
    transport = handler or httpx.MockTransport(lambda request: httpx.Response(500))
    now = clock if clock is not None else [0.0]
    return RemoteReranker(
        "http://reranker.test",
        model="m",
        max_documents=max_documents,
        client=httpx.AsyncClient(base_url="http://reranker.test", transport=transport),
        clock=lambda: now[0],
    )


def answer(*scores: tuple[int, float]) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "model": "m",
                "elapsed_ms": 5,
                "results": [{"index": i, "score": s} for i, s in scores],
            },
        )
    )


class TestRemoteReranker:
    async def test_sends_the_query_documents_and_top_k_and_squashes_the_scores(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "model": "m",
                    "elapsed_ms": 1,
                    "results": [{"index": 1, "score": 9.0}, {"index": 0, "score": -1.0}],
                },
            )

        ranked = await remote(httpx.MockTransport(handler)).rerank("q", ["a", "b", "c"], top_k=2)

        assert seen["path"] == "/v1/rerank"
        assert seen["body"] == {
            "query": "q",
            "documents": ["a", "b", "c"],
            "top_k": 2,
            "deadline_ms": 2700,  # 90% of the 3 s timeout, so the server stops before this client
        }
        assert [index for index, _ in ranked] == [1, 0]
        assert ranked[0][1] == pytest.approx(squash(9.0))
        assert all(0.0 < score < 1.0 for _, score in ranked)

    async def test_raw_scores_are_available_for_calibration(self) -> None:
        raw = await remote(answer((0, 7.25))).rerank_raw("q", ["a"], top_k=1)
        assert raw == [(0, 7.25)]

    async def test_top_k_is_capped_at_the_number_of_documents(self) -> None:
        bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"results": [{"index": 0, "score": 1.0}]})

        await remote(httpx.MockTransport(handler)).rerank("q", ["a"], top_k=50)
        assert bodies[0]["top_k"] == 1

    async def test_long_passages_are_cut_before_sending(self) -> None:
        bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"results": [{"index": 0, "score": 1.0}]})

        await remote(httpx.MockTransport(handler)).rerank("q", ["x" * 50_000], top_k=1)
        assert len(bodies[0]["documents"][0]) == MAX_DOCUMENT_CHARS  # type: ignore[index]

    async def test_nothing_to_rank_makes_no_request(self) -> None:
        reranker = remote()  # every request would fail
        assert await reranker.rerank("q", [], top_k=5) == []
        assert await reranker.rerank("q", ["a"], top_k=0) == []

    async def test_too_many_documents_are_refused_locally(self) -> None:
        with pytest.raises(RerankRejected, match="more than the 2 allowed"):
            await remote(answer(), max_documents=2).rerank("q", ["a", "b", "c"], top_k=1)

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_server_errors_and_a_busy_server_are_unavailable(self, status: int) -> None:
        reranker = remote(httpx.MockTransport(lambda request: httpx.Response(status)))
        with pytest.raises(RerankUnavailable):
            await reranker.rerank("q", ["a"], top_k=1)

    @pytest.mark.parametrize("status", [413, 422])
    async def test_client_errors_are_rejections_and_do_not_count_as_failures(
        self, status: int
    ) -> None:
        reranker = remote(httpx.MockTransport(lambda request: httpx.Response(status)))
        for _ in range(FAILURES_BEFORE_OPENING + 2):
            with pytest.raises(RerankRejected):
                await reranker.rerank("q", ["a"], top_k=1)

    @pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
    async def test_a_connection_failure_or_timeout_is_unavailable(self, error: Exception) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise error

        with pytest.raises(RerankUnavailable):
            await remote(httpx.MockTransport(handler)).rerank("q", ["a"], top_k=1)

    @pytest.mark.parametrize(
        "body",
        [
            {"nope": 1},
            {"results": [{"index": "x", "score": 1}]},
            {"results": [{"index": 5, "score": 1.0}]},
        ],
    )
    async def test_an_unreadable_or_out_of_range_answer_is_unavailable(
        self, body: dict[str, object]
    ) -> None:
        reranker = remote(httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
        with pytest.raises(RerankUnavailable):
            await reranker.rerank("q", ["a"], top_k=1)


class TestCircuitBreaker:
    async def fail_times(self, reranker: RemoteReranker, count: int) -> None:
        for _ in range(count):
            with pytest.raises(RerankUnavailable):
                await reranker.rerank("q", ["a"], top_k=1)

    async def test_repeated_failures_make_the_client_skip_the_call_for_a_while(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        reranker = remote(httpx.MockTransport(handler))
        await self.fail_times(reranker, FAILURES_BEFORE_OPENING)
        assert calls == FAILURES_BEFORE_OPENING

        await self.fail_times(reranker, 5)  # skipped without a request
        assert calls == FAILURES_BEFORE_OPENING

    async def test_it_tries_again_after_the_pause(self) -> None:
        clock = [0.0]
        healthy = [False]

        def handler(request: httpx.Request) -> httpx.Response:
            if healthy[0]:
                return httpx.Response(200, json={"results": [{"index": 0, "score": 2.0}]})
            return httpx.Response(503)

        reranker = remote(httpx.MockTransport(handler), clock=clock)
        await self.fail_times(reranker, FAILURES_BEFORE_OPENING)
        healthy[0] = True
        clock[0] = OPEN_FOR_SECONDS - 1
        await self.fail_times(reranker, 1)  # still paused

        clock[0] = OPEN_FOR_SECONDS + 1
        assert await reranker.rerank("q", ["a"], top_k=1)

    async def test_a_success_resets_the_count(self) -> None:
        outcomes = iter([503, 503, 200, 503, 503, 200])

        def handler(request: httpx.Request) -> httpx.Response:
            status = next(outcomes)
            if status == 200:
                return httpx.Response(200, json={"results": [{"index": 0, "score": 1.0}]})
            return httpx.Response(status)

        reranker = remote(httpx.MockTransport(handler))
        for expected_failures in (2, 2):
            await self.fail_times(reranker, expected_failures)
            assert await reranker.rerank("q", ["a"], top_k=1)  # never three failures in a row


class TestFactory:
    def settings(self, **overrides: object) -> Settings:
        base: dict[str, object] = {
            "database_url": "postgresql+asyncpg://u:p@localhost/db",
            "checkpoint_db_url": "postgresql://u:p@localhost/db",
            "redis_url": "redis://localhost:6379/0",
            "celery_result_backend": "redis://localhost:6379/1",
            "rabbitmq_url": "amqp://u:p@localhost:5672/v",
            "jwt_secret": "x" * 40,
            "llm_provider": "fake",
            "embedding_provider": "fake",
            "offline": False,
        }
        return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type, call-arg]

    def test_none_means_no_reranking(self) -> None:
        assert build_reranker(self.settings(rerank_provider="none")) is None

    def test_fake(self) -> None:
        assert isinstance(
            build_reranker(self.settings(rerank_provider="fake")), LexicalOverlapReranker
        )

    def test_cross_encoder_calls_the_separate_process(self) -> None:
        settings = self.settings(rerank_provider="cross_encoder", rerank_url="http://reranker:8100")
        reranker = build_reranker(settings)
        assert isinstance(reranker, RemoteReranker)
        assert reranker.model == ETTIN_RERANK_MODEL

    def test_cross_encoder_needs_no_voyage_key(self) -> None:
        settings = self.settings(rerank_provider="cross_encoder")
        assert settings.voyage_api_key is None

    def test_an_explicit_model_name_is_kept(self) -> None:
        settings = self.settings(rerank_provider="cross_encoder", rerank_model="my/other-model")
        assert settings.rerank_model == "my/other-model"

    def test_the_voyage_default_model_name_is_replaced_for_cross_encoder(self) -> None:
        settings = self.settings(rerank_provider="cross_encoder", rerank_model=VOYAGE_RERANK_MODEL)
        assert settings.rerank_model == ETTIN_RERANK_MODEL

    def test_there_is_no_voyage_reranker(self) -> None:
        with pytest.raises(ValidationError):
            self.settings(rerank_provider="voyage", voyage_api_key="pa-test")

    def test_the_timeout_and_url_come_from_settings(self) -> None:
        settings = self.settings(
            rerank_provider="cross_encoder", rerank_timeout_seconds=1.5, rerank_url="http://r:9"
        )
        reranker = build_reranker(settings)
        assert isinstance(reranker, RemoteReranker)
        assert reranker._client.timeout.read == 1.5
        assert str(reranker._client.base_url) == "http://r:9"
