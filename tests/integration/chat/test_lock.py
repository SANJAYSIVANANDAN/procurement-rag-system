"""The per-conversation lock against a real Redis (its Lua scripts need one)."""

import asyncio
from uuid import uuid4

from redis.asyncio import Redis

from covenantiq.core import redis_keys
from covenantiq.streaming.locks import ConversationLock


async def test_one_holder_at_a_time(redis_client: Redis, redis_env: str) -> None:
    lock = ConversationLock(redis_client, env=redis_env)
    conversation = uuid4()
    token = await lock.acquire(conversation)
    assert token is not None
    assert await lock.acquire(conversation) is None
    assert await lock.release(conversation, token) is True
    assert await lock.acquire(conversation) is not None


async def test_other_conversations_have_their_own_lock(redis_client: Redis, redis_env: str) -> None:
    lock = ConversationLock(redis_client, env=redis_env)
    assert await lock.acquire(uuid4()) is not None
    assert await lock.acquire(uuid4()) is not None


async def test_a_late_release_by_a_previous_holder_does_not_free_the_lock(
    redis_client: Redis, redis_env: str
) -> None:
    lock = ConversationLock(redis_client, env=redis_env, ttl_seconds=1)
    conversation = uuid4()
    old = await lock.acquire(conversation)
    assert old is not None
    await asyncio.sleep(1.2)  # the first holder was too slow: its lock expired
    new = await lock.acquire(conversation)
    assert new is not None

    assert await lock.release(conversation, old) is False  # compare-and-delete
    assert await lock.acquire(conversation) is None  # the new holder still has it
    assert await lock.release(conversation, new) is True


async def test_only_the_holder_can_extend(redis_client: Redis, redis_env: str) -> None:
    lock = ConversationLock(redis_client, env=redis_env, ttl_seconds=5)
    conversation = uuid4()
    token = await lock.acquire(conversation)
    assert token is not None
    key = redis_keys.conversation_lock(redis_env, conversation)
    await redis_client.pexpire(key, 500)

    assert await lock.extend(conversation, "someone-else") is False
    assert await redis_client.pttl(key) <= 500
    assert await lock.extend(conversation, token) is True
    assert await redis_client.pttl(key) > 4000


async def test_an_abandoned_lock_expires_by_itself(redis_client: Redis, redis_env: str) -> None:
    lock = ConversationLock(redis_client, env=redis_env, ttl_seconds=1)
    conversation = uuid4()
    assert await lock.acquire(conversation) is not None
    await asyncio.sleep(1.2)
    assert await lock.acquire(conversation) is not None
