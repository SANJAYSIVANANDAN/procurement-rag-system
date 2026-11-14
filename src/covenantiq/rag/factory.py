from collections.abc import Callable

from redis.asyncio import Redis

from covenantiq.ai.embeddings.factory import build_embedding_provider
from covenantiq.ai.rerank.factory import build_reranker
from covenantiq.core.config import Settings
from covenantiq.db.uow import UnitOfWork
from covenantiq.rag.cache import Cache, NullCache, RedisCache
from covenantiq.rag.config import RagConfig, load_rag_config
from covenantiq.rag.retrieval import RetrievalEngine
from covenantiq.rag.session import search_session_factory


def build_retrieval_engine(
    settings: Settings,
    uow_factory: Callable[[], UnitOfWork],
    *,
    redis: Redis | None = None,
    config: RagConfig | None = None,
) -> RetrievalEngine:
    """The engine as the application runs it: the configured embedding provider and reranker (or
    none), the tuning from `config/rag.yaml` with the environment on top, and a Redis cache that
    fails open. The caller closes the engine with `aclose()`."""
    cache: Cache = RedisCache(redis) if redis is not None else NullCache()
    return RetrievalEngine(
        search_session_factory(uow_factory),
        build_embedding_provider(settings),
        build_reranker(settings),
        (config or load_rag_config(settings.rag_config_file)).with_settings(settings),
        cache=cache,
        env=settings.env.value,
        exact_scan_threshold=settings.exact_scan_threshold,
    )
