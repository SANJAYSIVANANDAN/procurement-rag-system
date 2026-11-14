import json
import math
from collections.abc import Sequence

import httpx
import pytest

from covenantiq.ai.embeddings.base import (
    EmbeddingAuthFailed,
    EmbeddingRejected,
    EmbeddingUnavailable,
)
from covenantiq.ai.embeddings.factory import build_embedding_provider
from covenantiq.ai.embeddings.fake import HashEmbeddingProvider
from covenantiq.ai.embeddings.openai_compat import OpenAICompatEmbeddingProvider
from covenantiq.ai.embeddings.retry import RetryingEmbeddingProvider
from covenantiq.core.config import Settings


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class TestHashProvider:
    async def test_vectors_are_deterministic_unit_length_and_the_right_size(self) -> None:
        provider = HashEmbeddingProvider(dim=64)
        first = (await provider.embed_documents(["limitation of liability"]))[0]
        again = await provider.embed_query("limitation of liability")
        assert first == again
        assert len(first) == 64
        assert math.isclose(math.sqrt(sum(v * v for v in first)), 1.0, rel_tol=1e-9)

    async def test_texts_sharing_words_are_closer_than_unrelated_texts(self) -> None:
        provider = HashEmbeddingProvider(dim=256)
        a, b, c = await provider.embed_documents(
            ["total liability is limited to fees", "liability is limited", "weather forecast rain"]
        )
        assert cosine(a, b) > cosine(a, c)

    async def test_empty_text_still_gives_a_valid_vector(self) -> None:
        vector = (await HashEmbeddingProvider(dim=8).embed_documents([""]))[0]
        assert math.isclose(sum(v * v for v in vector), 1.0)

    async def test_calls_are_recorded(self) -> None:
        provider = HashEmbeddingProvider(dim=8)
        await provider.embed_documents(["a", "b"])
        assert provider.calls == [["a", "b"]]


class Flaky:
    model = "m"
    dim = 2

    def __init__(self, failures: int, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error or EmbeddingUnavailable("down")
        self.attempts = 0

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise self.error
        return [[1.0, 0.0] for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]

    async def aclose(self) -> None:
        return None


class TestRetry:
    def wrap(
        self, inner: Flaky, attempts: int = 4
    ) -> tuple[RetryingEmbeddingProvider, list[float]]:
        delays: list[float] = []

        async def sleep(seconds: float) -> None:
            delays.append(seconds)

        return RetryingEmbeddingProvider(inner, attempts=attempts, sleep=sleep), delays

    async def test_transient_failures_are_retried_with_growing_delays(self) -> None:
        inner = Flaky(failures=2)
        provider, delays = self.wrap(inner)
        assert await provider.embed_documents(["x"]) == [[1.0, 0.0]]
        assert inner.attempts == 3
        assert len(delays) == 2
        assert delays[1] > delays[0] * 0.9  # exponential, with jitter

    async def test_gives_up_after_the_configured_attempts(self) -> None:
        inner = Flaky(failures=99)
        provider, _ = self.wrap(inner, attempts=3)
        with pytest.raises(EmbeddingUnavailable):
            await provider.embed_documents(["x"])
        assert inner.attempts == 3

    @pytest.mark.parametrize("error", [EmbeddingRejected("bad"), EmbeddingAuthFailed("key")])
    async def test_permanent_errors_are_not_retried(self, error: Exception) -> None:
        inner = Flaky(failures=99, error=error)
        provider, delays = self.wrap(inner)
        with pytest.raises(type(error)):
            await provider.embed_documents(["x"])
        assert inner.attempts == 1
        assert delays == []

    async def test_queries_are_retried_too_and_metadata_is_forwarded(self) -> None:
        inner = Flaky(failures=1)
        provider, _ = self.wrap(inner)
        assert await provider.embed_query("x") == [1.0, 0.0]
        assert (provider.model, provider.dim) == ("m", 2)


def compat(
    handler: httpx.MockTransport, dim: int = 3, *, request_dimensions: bool = False
) -> OpenAICompatEmbeddingProvider:
    client = httpx.AsyncClient(base_url="http://embed.test/v1", transport=handler)
    return OpenAICompatEmbeddingProvider(
        "http://embed.test/v1",
        "k",
        "local-model",
        dim,
        request_dimensions=request_dimensions,
        client=client,
    )


def ok_body(rows: list[list[float]]) -> dict[str, object]:
    # deliberately out of order: the adapter must sort by index
    return {"data": [{"index": i, "embedding": row} for i, row in reversed(list(enumerate(rows)))]}


class TestOpenAICompat:
    async def test_posts_the_batch_and_orders_vectors_by_index(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=ok_body([[1, 0, 0], [0, 1, 0]]))

        provider = compat(httpx.MockTransport(handler))
        vectors = await provider.embed_documents(["a", "b"])
        assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        assert seen["path"] == "/v1/embeddings"
        assert seen["body"] == {"model": "local-model", "input": ["a", "b"]}

    async def test_dimensions_are_requested_only_when_asked_for(self) -> None:
        bodies: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=ok_body([[1, 0, 0]]))

        await compat(httpx.MockTransport(handler)).embed_documents(["a"])
        await compat(httpx.MockTransport(handler), request_dimensions=True).embed_documents(["a"])
        await compat(httpx.MockTransport(handler), request_dimensions=True).embed_query("a")

        assert "dimensions" not in bodies[0]  # a local server may reject the field
        assert bodies[1]["dimensions"] == 3
        assert bodies[2]["dimensions"] == 3  # queries must be the same size as documents

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, EmbeddingAuthFailed),
            (403, EmbeddingAuthFailed),
            (429, EmbeddingUnavailable),
            (503, EmbeddingUnavailable),
            (400, EmbeddingRejected),
            (422, EmbeddingRejected),
        ],
    )
    async def test_http_errors_map_to_the_right_kind(
        self, status: int, expected: type[Exception]
    ) -> None:
        provider = compat(httpx.MockTransport(lambda request: httpx.Response(status)))
        with pytest.raises(expected):
            await provider.embed_documents(["a"])

    async def test_a_connection_failure_is_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(EmbeddingUnavailable):
            await compat(httpx.MockTransport(handler)).embed_documents(["a"])

    async def test_a_wrong_dimension_or_count_is_rejected(self) -> None:
        wrong_dim = compat(
            httpx.MockTransport(lambda r: httpx.Response(200, json=ok_body([[1, 0]])))
        )
        with pytest.raises(EmbeddingRejected, match="unexpected shape"):
            await wrong_dim.embed_documents(["a"])
        wrong_count = compat(
            httpx.MockTransport(lambda r: httpx.Response(200, json=ok_body([[1, 0, 0]])))
        )
        with pytest.raises(EmbeddingRejected, match="unexpected shape"):
            await wrong_count.embed_documents(["a", "b"])

    async def test_an_unreadable_body_is_rejected(self) -> None:
        provider = compat(httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": 1})))
        with pytest.raises(EmbeddingRejected, match="unreadable"):
            await provider.embed_documents(["a"])


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
            "offline": False,  # an OFFLINE variable leaked from another test must not matter
        }
        return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type]

    def test_fake_provider(self) -> None:
        provider = build_embedding_provider(
            self.settings(embedding_provider="fake", rerank_provider="none")
        )
        assert isinstance(provider, HashEmbeddingProvider)
        assert provider.dim == 1024

    def test_voyage_is_wrapped_for_retries(self) -> None:
        provider = build_embedding_provider(
            self.settings(
                embedding_provider="voyage", voyage_api_key="pa-test-key", rerank_provider="none"
            )
        )
        assert isinstance(provider, RetryingEmbeddingProvider)
        assert (provider.model, provider.dim) == ("voyage-law-2", 1024)

    def test_text_embedding_3_small_is_asked_for_the_column_width(self) -> None:
        provider = build_embedding_provider(
            self.settings(
                embedding_provider="openai_compat",
                embedding_model="text-embedding-3-small",
                embedding_dim=1024,
                openai_compat_base_url="https://api.openai.com/v1",
                openai_compat_api_key="sk-test",
                rerank_provider="none",
            )
        )
        assert isinstance(provider, RetryingEmbeddingProvider)
        inner = provider._inner
        assert isinstance(inner, OpenAICompatEmbeddingProvider)
        assert (inner.model, inner.dim, inner._request_dimensions) == (
            "text-embedding-3-small",
            1024,
            True,
        )

    def test_a_local_model_is_not_sent_the_dimensions_field(self) -> None:
        provider = build_embedding_provider(
            self.settings(
                embedding_provider="openai_compat",
                embedding_model="local",
                openai_compat_base_url="http://localhost:8000/v1",
                openai_compat_api_key="k",
                rerank_provider="none",
            )
        )
        assert isinstance(provider, RetryingEmbeddingProvider)
        assert provider._inner._request_dimensions is False  # type: ignore[attr-defined]

    def test_asking_for_more_dimensions_than_the_model_has_fails_at_startup(self) -> None:
        with pytest.raises(ValueError, match="at most 1536 dimensions"):
            self.settings(
                embedding_provider="openai_compat",
                embedding_model="text-embedding-3-small",
                embedding_dim=2048,
                openai_compat_base_url="https://api.openai.com/v1",
                openai_compat_api_key="sk-test",
                rerank_provider="none",
            )

    def test_the_standard_openai_key_variable_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-environment")
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url="postgresql+asyncpg://u:p@localhost/db",
            checkpoint_db_url="postgresql://u:p@localhost/db",
            redis_url="redis://localhost:6379/0",
            celery_result_backend="redis://localhost:6379/1",
            rabbitmq_url="amqp://u:p@localhost:5672/v",
            jwt_secret="x" * 40,
            llm_provider="fake",
            offline=False,
            embedding_provider="openai_compat",
            embedding_model="text-embedding-3-small",
            openai_compat_base_url="https://api.openai.com/v1",
            rerank_provider="none",
        )
        assert settings.openai_compat_api_key is not None
        assert settings.openai_compat_api_key.get_secret_value() == "sk-from-environment"

    def test_openai_compat_is_wrapped_for_retries(self) -> None:
        provider = build_embedding_provider(
            self.settings(
                embedding_provider="openai_compat",
                embedding_model="local",
                openai_compat_base_url="http://localhost:8000/v1",
                openai_compat_api_key="k",
                rerank_provider="none",
            )
        )
        assert isinstance(provider, RetryingEmbeddingProvider)
        assert provider.model == "local"
