import json

import httpx
import pytest

from covenantiq.ai.rerank import RerankRejected, RerankUnavailable, build_reranker
from covenantiq.ai.rerank.breaker import FAILURES_BEFORE_OPENING, OPEN_FOR_SECONDS
from covenantiq.ai.rerank.cohere import MAX_TOKENS_PER_DOC, CohereReranker
from covenantiq.core.config import (
    COHERE_RERANK_MODEL,
    ETTIN_RERANK_MODEL,
    VOYAGE_RERANK_MODEL,
    Settings,
)

SECRET = "co-test-key-do-not-log"


def cohere(
    handler: httpx.MockTransport | None = None,
    *,
    clock: list[float] | None = None,
    max_documents: int = 100,
) -> CohereReranker:
    transport = handler or httpx.MockTransport(lambda request: httpx.Response(500))
    now = clock if clock is not None else [0.0]
    return CohereReranker(
        SECRET,
        model="rerank-v5.0-fast",
        max_documents=max_documents,
        client=httpx.AsyncClient(
            base_url="https://api.cohere.test",
            headers={"Authorization": f"Bearer {SECRET}"},
            transport=transport,
        ),
        clock=lambda: now[0],
    )


def reply(*pairs: tuple[int, float]) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "id": "x",
                "results": [{"index": i, "relevance_score": s} for i, s in pairs],
                "meta": {"billed_units": {"search_units": 1}},
            },
        )
    )


class TestRequestAndResponse:
    async def test_sends_the_v2_rerank_request_with_the_key_and_nothing_identifying(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["auth"] = request.headers["authorization"]
            seen["headers"] = {name.lower() for name in request.headers}
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.91}]})

        await cohere(httpx.MockTransport(handler)).rerank("liability cap", ["a", "b", "c"], top_k=1)

        assert seen["path"] == "/v2/rerank"
        assert seen["auth"] == f"Bearer {SECRET}"
        assert seen["body"] == {
            "model": "rerank-v5.0-fast",
            "query": "liability cap",
            "documents": ["a", "b", "c"],
            "top_n": 1,
            "max_tokens_per_doc": MAX_TOKENS_PER_DOC,
        }
        assert "x-client-name" not in seen["headers"]  # type: ignore[operator]

    async def test_scores_are_returned_as_cohere_gives_them_best_first(self) -> None:
        ranked = await cohere(reply((0, 0.2), (2, 0.93), (1, 0.55))).rerank(
            "q", ["a", "b", "c"], top_k=3
        )
        assert ranked == [(2, 0.93), (1, 0.55), (0, 0.2)]  # already 0-1: no sigmoid

    async def test_top_k_is_capped_at_the_number_of_documents(self) -> None:
        bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.5}]})

        await cohere(httpx.MockTransport(handler)).rerank("q", ["a"], top_k=50)
        assert bodies[0]["top_n"] == 1

    async def test_nothing_to_rank_makes_no_request(self) -> None:
        reranker = cohere()  # every request would fail
        assert await reranker.rerank("q", [], top_k=5) == []
        assert await reranker.rerank("q", ["a"], top_k=0) == []

    async def test_too_many_documents_are_refused_locally(self) -> None:
        with pytest.raises(RerankRejected, match="more than the 2 allowed"):
            await cohere(reply(), max_documents=2).rerank("q", ["a", "b", "c"], top_k=1)


class TestFailures:
    @pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 504])
    async def test_auth_rate_limit_and_server_errors_are_unavailable(self, status: int) -> None:
        reranker = cohere(httpx.MockTransport(lambda request: httpx.Response(status)))
        with pytest.raises(RerankUnavailable):
            await reranker.rerank("q", ["a"], top_k=1)

    @pytest.mark.parametrize("status", [400, 404, 422])
    async def test_bad_requests_are_rejections_and_do_not_open_the_breaker(
        self, status: int
    ) -> None:
        reranker = cohere(httpx.MockTransport(lambda request: httpx.Response(status)))
        for _ in range(FAILURES_BEFORE_OPENING + 2):
            with pytest.raises(RerankRejected):
                await reranker.rerank("q", ["a"], top_k=1)

    @pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
    async def test_a_timeout_or_connection_failure_is_unavailable(self, error: Exception) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise error

        with pytest.raises(RerankUnavailable):
            await cohere(httpx.MockTransport(handler)).rerank("q", ["a"], top_k=1)

    @pytest.mark.parametrize(
        "body",
        [
            {"nope": 1},
            {"results": [{"index": 0}]},
            {"results": [{"index": "x", "relevance_score": 1}]},
            {"results": [{"index": 9, "relevance_score": 0.4}]},
        ],
    )
    async def test_an_unreadable_or_out_of_range_answer_is_unavailable(
        self, body: dict[str, object]
    ) -> None:
        reranker = cohere(httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
        with pytest.raises(RerankUnavailable):
            await reranker.rerank("q", ["a"], top_k=1)

    async def test_the_api_key_never_appears_in_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach host with {request.headers['authorization']}")

        for transport in (
            httpx.MockTransport(handler),
            httpx.MockTransport(lambda request: httpx.Response(401, text=SECRET)),
        ):
            with pytest.raises(RerankUnavailable) as caught:
                await cohere(transport).rerank("q", ["a"], top_k=1)
            assert SECRET not in str(caught.value)
            assert SECRET not in repr(caught.value)


class TestBreaker:
    async def test_repeated_failures_make_the_client_skip_the_call_for_a_while(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429)

        reranker = cohere(httpx.MockTransport(handler))
        for _ in range(FAILURES_BEFORE_OPENING + 4):
            with pytest.raises(RerankUnavailable):
                await reranker.rerank("q", ["a"], top_k=1)
        assert calls == FAILURES_BEFORE_OPENING  # the rest never left the process

    async def test_it_tries_again_after_the_pause_and_recovers(self) -> None:
        clock = [0.0]
        healthy = [False]

        def handler(request: httpx.Request) -> httpx.Response:
            if healthy[0]:
                return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.7}]})
            return httpx.Response(503)

        reranker = cohere(httpx.MockTransport(handler), clock=clock)
        for _ in range(FAILURES_BEFORE_OPENING):
            with pytest.raises(RerankUnavailable):
                await reranker.rerank("q", ["a"], top_k=1)
        healthy[0] = True
        clock[0] = OPEN_FOR_SECONDS + 1
        assert await reranker.rerank("q", ["a"], top_k=1) == [(0, 0.7)]

    async def test_a_rejected_key_stops_every_query_from_calling_out(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(401)

        reranker = cohere(httpx.MockTransport(handler))
        for _ in range(10):
            with pytest.raises(RerankUnavailable):
                await reranker.rerank("q", ["a"], top_k=1)
        assert calls == FAILURES_BEFORE_OPENING


class TestSettingsAndFactory:
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

    def test_cohere_needs_its_key(self) -> None:
        with pytest.raises(ValueError, match="COHERE_API_KEY is required"):
            self.settings(rerank_provider="cohere")

    def test_the_default_model_is_the_low_latency_one(self) -> None:
        settings = self.settings(rerank_provider="cohere", cohere_api_key=SECRET)
        assert settings.rerank_model == COHERE_RERANK_MODEL == "rerank-v5.0-fast"

    def test_the_voyage_default_or_a_blank_name_means_the_providers_default(self) -> None:
        for model in (VOYAGE_RERANK_MODEL, "", "  "):
            settings = self.settings(
                rerank_provider="cohere", cohere_api_key=SECRET, rerank_model=model
            )
            assert settings.rerank_model == COHERE_RERANK_MODEL
        blank_for_ettin = self.settings(rerank_provider="cross_encoder", rerank_model="")
        assert blank_for_ettin.rerank_model == ETTIN_RERANK_MODEL
        assert (
            self.settings(rerank_provider="none", rerank_model="").rerank_model
            == VOYAGE_RERANK_MODEL
        )

    def test_an_explicit_model_is_kept(self) -> None:
        settings = self.settings(
            rerank_provider="cohere", cohere_api_key=SECRET, rerank_model="rerank-v5.0-pro"
        )
        assert settings.rerank_model == "rerank-v5.0-pro"

    @pytest.mark.parametrize("variable", ["CIQ_COHERE_API_KEY", "COHERE_API_KEY", "CO_API_KEY"])
    def test_the_key_is_read_from_the_usual_variables(
        self, monkeypatch: pytest.MonkeyPatch, variable: str
    ) -> None:
        for name in ("CIQ_COHERE_API_KEY", "COHERE_API_KEY", "CO_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(variable, SECRET)
        settings = self.settings(rerank_provider="cohere")
        assert settings.cohere_api_key is not None
        assert settings.cohere_api_key.get_secret_value() == SECRET

    def test_a_blank_key_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COHERE_API_KEY", "")
        with pytest.raises(ValueError, match="COHERE_API_KEY is required"):
            self.settings(rerank_provider="cohere")

    def test_the_key_is_not_shown_when_settings_are_printed(self) -> None:
        settings = self.settings(rerank_provider="cohere", cohere_api_key=SECRET)
        assert SECRET not in repr(settings)
        assert SECRET not in str(settings.model_dump())

    def test_the_factory_builds_the_cohere_reranker_with_the_configured_limits(self) -> None:
        settings = self.settings(
            rerank_provider="cohere",
            cohere_api_key=SECRET,
            rerank_timeout_seconds=2.0,
            rerank_max_documents=40,
        )
        reranker = build_reranker(settings)
        assert isinstance(reranker, CohereReranker)
        assert reranker.model == "rerank-v5.0-fast"
        assert reranker._client.timeout.read == 2.0
        assert reranker._max_documents == 40
        assert str(reranker._client.base_url) == "https://api.cohere.com"
