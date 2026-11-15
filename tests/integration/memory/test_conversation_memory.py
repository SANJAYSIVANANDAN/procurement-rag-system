"""Memory through the conversation service: the ownership check comes before Redis, and the factory
applies the settings."""

from typing import Any
from uuid import UUID

import pytest
from redis.asyncio import Redis

from covenantiq.ai.llm.fake import FakeLLMProvider
from covenantiq.core import redis_keys
from covenantiq.core.config import Settings
from covenantiq.core.errors import NotFound
from covenantiq.db.scope import OwnershipScope
from covenantiq.memory import (
    MemoryManager,
    Reconstructor,
    SessionMemoryProvider,
)
from covenantiq.memory.factory import build_memory_manager
from covenantiq.services.conversation_service import ConversationService
from covenantiq.services.dto import Principal
from tests.integration.memory.helpers import (
    UowFactory,
    build,
    exchange,
    new_conversation,
    new_user,
    texts,
)


def principal(scope: OwnershipScope) -> Principal:
    return Principal(user_id=scope.owner_id, email="user@example.com")


async def session_keys(redis: Redis, env: str) -> list[str]:
    return [k.decode() async for k in redis.scan_iter(match=f"ciq:{env}:sess:*")]


class TestOwnershipBeforeRedis:
    async def test_the_owner_gets_the_conversations_memory(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str
    ) -> None:
        memory = build(uow_factory, redis_client, redis_env)
        service = ConversationService(uow_factory, memory.manager)
        alice = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, alice)
        done = await exchange(uow_factory, alice, conversation, "Question?", "Answer.")
        await memory.manager.record_turn(alice, conversation, done)

        context = await service.memory_context(principal(alice), conversation)

        assert texts(context.recent_turns) == ["Question?", "Answer."]

    async def test_someone_elses_conversation_is_404_and_redis_is_never_touched(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str
    ) -> None:
        memory = build(uow_factory, redis_client, redis_env)
        service = ConversationService(uow_factory, memory.manager)
        alice = await new_user(uow_factory, "alice@example.com")
        mallory = await new_user(uow_factory, "mallory@example.com")
        conversation = await new_conversation(uow_factory, alice)
        done = await exchange(uow_factory, alice, conversation, "Secret question", "Secret answer")
        await memory.manager.record_turn(alice, conversation, done)
        before = await session_keys(redis_client, redis_env)

        with pytest.raises(NotFound):
            await service.memory_context(principal(mallory), conversation)

        assert await session_keys(redis_client, redis_env) == before  # nothing created or read
        assert not [k for k in before if str(mallory.owner_id) in k]

    async def test_an_unknown_conversation_is_404_and_creates_no_keys(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str
    ) -> None:
        memory = build(uow_factory, redis_client, redis_env)
        service = ConversationService(uow_factory, memory.manager)
        alice = await new_user(uow_factory)
        with pytest.raises(NotFound):
            await service.memory_context(principal(alice), UUID(int=99))
        assert await session_keys(redis_client, redis_env) == []

    async def test_a_service_without_memory_says_so(self, uow_factory: UowFactory) -> None:
        alice = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, alice)
        with pytest.raises(RuntimeError, match="not configured"):
            await ConversationService(uow_factory).memory_context(principal(alice), conversation)


class TestFactory:
    def settings(self, **extra: Any) -> Settings:
        base: dict[str, Any] = {
            "database_url": "postgresql+asyncpg://u:p@localhost/db",
            "checkpoint_db_url": "postgresql://u:p@localhost/db",
            "redis_url": "redis://localhost:6379/0",
            "celery_result_backend": "redis://localhost:6379/1",
            "rabbitmq_url": "amqp://u:p@localhost:5672/v",
            "jwt_secret": "x" * 40,
            "offline": True,
        }
        return Settings(_env_file=None, **{**base, **extra})  # type: ignore[arg-type, call-arg]

    async def test_settings_decide_the_budgets_ttl_and_key_prefix(
        self, uow_factory: UowFactory, redis_client: Redis
    ) -> None:
        settings = self.settings(
            session_ttl_seconds=1234, memory_window_tokens=700, memory_summary_tokens=150
        )
        manager = build_memory_manager(settings, uow_factory, redis_client, FakeLLMProvider())
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        done = await exchange(uow_factory, scope, conversation, "Q", "A")
        await manager.record_turn(scope, conversation, done)  # cold: rebuilt, so keys are written
        state = await Reconstructor(uow_factory, 700).state(scope, conversation)
        assert state is not None
        await manager.load(scope, state)

        window = redis_keys.session_window(settings.env.value, scope.owner_id, conversation)
        try:
            assert 0 < await redis_client.ttl(window) <= 1234
            assert isinstance(manager, MemoryManager)
            session = manager._session
            assert isinstance(session, SessionMemoryProvider)
            assert session._budget == 700
            assert manager._summarizer._summary_tokens == 150
        finally:
            await redis_client.delete(
                window, redis_keys.session_meta(settings.env.value, scope.owner_id, conversation)
            )
