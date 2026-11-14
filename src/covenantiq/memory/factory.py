from collections.abc import Callable

from redis.asyncio import Redis

from covenantiq.ai.llm.base import LLMProvider
from covenantiq.core.config import Settings
from covenantiq.db.uow import UnitOfWork
from covenantiq.memory.manager import MemoryManager
from covenantiq.memory.reconstruction import Reconstructor
from covenantiq.memory.session_redis import SessionMemoryProvider
from covenantiq.memory.summarizer import Summarizer


def build_memory_manager(
    settings: Settings,
    uow_factory: Callable[[], UnitOfWork],
    redis: Redis,
    llm: LLMProvider,
) -> MemoryManager:
    """The memory manager as the application runs it: the session provider (the only one in V1),
    sized and expiring according to settings, with the model that writes the summaries."""
    reconstructor = Reconstructor(uow_factory, settings.memory_window_tokens)
    session = SessionMemoryProvider(
        redis,
        reconstructor,
        env=settings.env.value,
        ttl_seconds=settings.session_ttl_seconds,
        window_tokens=settings.memory_window_tokens,
    )
    summarizer = Summarizer(llm, summary_tokens=settings.memory_summary_tokens)
    return MemoryManager(session, summarizer, uow_factory)
