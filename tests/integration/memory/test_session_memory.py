"""Session memory against real Redis and PostgreSQL (spec 23, milestone 10 acceptance).

Covers: the window after Redis loses everything, a stale `last_seq`, corrupt or old-format entries,
overflow into a summary stored in PostgreSQL, the key layout and its TTLs, and two users never
sharing a session. Needs CIQ_TEST_DATABASE_URL and CIQ_TEST_REDIS_URL.
"""

import asyncio
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from covenantiq.ai.llm.base import LLMUnavailable
from covenantiq.core import redis_keys
from covenantiq.core.enums import MessageStatus
from covenantiq.memory import CompletedTurn, SessionTurn
from tests.integration.memory.helpers import (
    Memory,
    UowFactory,
    build,
    exchange,
    new_conversation,
    new_user,
    shape,
    state_of,
    texts,
    turn,
)

PAIRS = [
    ("What is the liability cap?", "It is the fees paid in twelve months."),
    ("Are there carve-outs?", "Yes, for confidentiality and data protection."),
    ("What about termination?", "Either party may terminate for material breach."),
]


@pytest.fixture
def memory(uow_factory: UowFactory, redis_client: Redis, redis_env: str) -> Memory:
    return build(uow_factory, redis_client, redis_env)


async def converse(
    uow_factory: UowFactory,
    memory: Memory,
    scope,
    conversation_id,
    pairs=PAIRS,  # type: ignore[no-untyped-def]
) -> list[CompletedTurn]:
    """Store exchanges in PostgreSQL and record each in memory, as the chat service will."""
    done = []
    for question, answer in pairs:
        completed = await exchange(uow_factory, scope, conversation_id, question, answer)
        await memory.manager.record_turn(scope, conversation_id, completed)
        done.append(completed)
    await memory.manager.aclose()
    return done


class TestKeysAndTtl:
    async def test_the_keys_carry_the_user_id_the_env_prefix_and_a_ttl(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str
    ) -> None:
        memory = build(uow_factory, redis_client, redis_env, ttl=900)
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(uow_factory, memory, scope, conversation)

        window = redis_keys.session_window(redis_env, scope.owner_id, conversation)
        meta = redis_keys.session_meta(redis_env, scope.owner_id, conversation)
        assert window == f"ciq:{redis_env}:sess:{scope.owner_id}:{conversation}:window"
        assert str(scope.owner_id) in window and str(scope.owner_id) in meta
        assert await redis_client.type(window) == b"list"
        assert await redis_client.type(meta) == b"hash"
        for key in (window, meta):
            assert 0 < await redis_client.ttl(key) <= 900

    async def test_a_read_refreshes_the_ttl_of_both_keys(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str
    ) -> None:
        memory = build(uow_factory, redis_client, redis_env, ttl=900)
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(uow_factory, memory, scope, conversation)
        window = redis_keys.session_window(redis_env, scope.owner_id, conversation)
        meta = redis_keys.session_meta(redis_env, scope.owner_id, conversation)
        await redis_client.expire(window, 5)
        await redis_client.expire(meta, 5)

        await memory.manager.load(scope, await state_of(memory, scope, conversation))

        for key in (window, meta):
            assert await redis_client.ttl(key) > 800

    async def test_the_session_keys_are_the_only_ones_created(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str, memory: Memory
    ) -> None:
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(uow_factory, memory, scope, conversation)
        keys = [k.decode() async for k in redis_client.scan_iter(match=f"ciq:{redis_env}:*")]
        assert sorted(k.rsplit(":", 1)[1] for k in keys) == ["meta", "window"]


class TestLoad:
    async def test_the_window_holds_the_turns_in_order(
        self, uow_factory: UowFactory, memory: Memory
    ) -> None:
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(uow_factory, memory, scope, conversation)

        context = await memory.manager.load(scope, await state_of(memory, scope, conversation))

        assert texts(context.recent_turns) == [t for pair in PAIRS for t in pair]
        assert [t.role for t in context.recent_turns] == ["user", "assistant"] * 3
        assert [t.seq for t in context.recent_turns] == [1, 2, 3, 4, 5, 6]
        assert context.summary is None

    async def test_deleting_the_redis_keys_gives_the_same_window_back(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str, memory: Memory
    ) -> None:
        """Spec acceptance: Redis can lose everything and the answer does not change."""
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(uow_factory, memory, scope, conversation)
        before = await memory.manager.load(scope, await state_of(memory, scope, conversation))

        await redis_client.delete(
            redis_keys.session_window(redis_env, scope.owner_id, conversation),
            redis_keys.session_meta(redis_env, scope.owner_id, conversation),
        )
        after = await memory.manager.load(scope, await state_of(memory, scope, conversation))

        assert shape(after.recent_turns) == shape(before.recent_turns)
        assert after.summary == before.summary
        # and it is back in Redis for the next read
        assert (
            await redis_client.llen(
                redis_keys.session_window(redis_env, scope.owner_id, conversation)
            )
            == 6
        )

    async def test_a_flushed_redis_is_the_same_as_deleted_keys(
        self, uow_factory: UowFactory, redis_client: Redis, memory: Memory
    ) -> None:
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(uow_factory, memory, scope, conversation)
        before = await memory.manager.load(scope, await state_of(memory, scope, conversation))
        # a restart: everything this test created is gone
        keys = [k async for k in redis_client.scan_iter(match="ciq:*:sess:*")]
        await redis_client.delete(*keys)
        after = await memory.manager.load(scope, await state_of(memory, scope, conversation))
        assert shape(after.recent_turns) == shape(before.recent_turns)

    async def test_an_empty_conversation_loads_an_empty_window(
        self, uow_factory: UowFactory, memory: Memory
    ) -> None:
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        context = await memory.manager.load(scope, await state_of(memory, scope, conversation))
        assert context.recent_turns == [] and context.summary is None

    async def test_a_failed_exchange_is_not_part_of_memory(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str, memory: Memory
    ) -> None:
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(uow_factory, memory, scope, conversation, PAIRS[:1])
        await exchange(
            uow_factory, scope, conversation, "A question that failed", "", complete=False
        )
        await converse(uow_factory, memory, scope, conversation, PAIRS[1:2])

        # rebuilt from PostgreSQL: the user message of the failed exchange was stored as completed
        # content, the reply never completed, and the sequence numbers skip the gap
        await redis_client.delete(
            redis_keys.session_window(redis_env, scope.owner_id, conversation),
            redis_keys.session_meta(redis_env, scope.owner_id, conversation),
        )
        context = await memory.manager.load(scope, await state_of(memory, scope, conversation))
        assert "" not in texts(context.recent_turns)
        assert PAIRS[1][1] in texts(context.recent_turns)
        assert all(t.seq in (1, 2, 3, 5, 6) for t in context.recent_turns)


class TestRebuildTriggers:
    async def rebuild_log(self, memory: Memory, scope, conversation) -> list[str]:  # type: ignore[no-untyped-def]
        loaded = await memory.provider.load_session(
            scope, await state_of(memory, scope, conversation)
        )
        return [loaded.rebuilt_because or "none"]

    async def test_a_last_seq_mismatch_triggers_a_rebuild_that_includes_the_missing_turn(
        self, uow_factory: UowFactory, memory: Memory
    ) -> None:
        """Spec acceptance. A reply was committed to PostgreSQL but never recorded in Redis (the
        process died between the two): the window is behind, and the check notices."""
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(uow_factory, memory, scope, conversation, PAIRS[:2])
        await exchange(
            uow_factory, scope, conversation, "Newest question", "Newest answer"
        )  # not recorded

        loaded = await memory.provider.load_session(
            scope, await state_of(memory, scope, conversation)
        )

        assert loaded.rebuilt_because == "last_seq_mismatch"
        assert texts(loaded.context.recent_turns)[-2:] == ["Newest question", "Newest answer"]
        # and the rebuilt window is consistent from then on
        again = await memory.provider.load_session(
            scope, await state_of(memory, scope, conversation)
        )
        assert again.rebuilt_because is None

    async def test_a_consistent_session_is_not_rebuilt(
        self, uow_factory: UowFactory, memory: Memory
    ) -> None:
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(uow_factory, memory, scope, conversation)
        assert await self.rebuild_log(memory, scope, conversation) == ["none"]

    @pytest.mark.parametrize(
        ("damage", "reason"),
        [
            ("corrupt_turn", "corrupt_turn"),
            ("corrupt_json", "corrupt_turn"),
            ("schema", "schema_version"),
            ("no_schema", "schema_version"),
            ("bad_last_seq", "corrupt_meta"),
            ("tokens", "token_count_mismatch"),
            ("order", "out_of_order"),
            ("summary_step", "summary_out_of_step"),
        ],
    )
    async def test_damaged_or_outdated_entries_are_rebuilt_not_trusted(
        self,
        uow_factory: UowFactory,
        redis_client: Redis,
        redis_env: str,
        memory: Memory,
        damage: str,
        reason: str,
    ) -> None:
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(uow_factory, memory, scope, conversation)
        before = await memory.manager.load(scope, await state_of(memory, scope, conversation))
        window = redis_keys.session_window(redis_env, scope.owner_id, conversation)
        meta = redis_keys.session_meta(redis_env, scope.owner_id, conversation)

        if damage == "corrupt_turn":
            await redis_client.rpush(window, '{"seq": "not a number"}')
        elif damage == "corrupt_json":
            await redis_client.lset(window, 0, "{this is not json")
        elif damage == "schema":
            await redis_client.hset(meta, "schema_v", 99)
        elif damage == "no_schema":
            await redis_client.hdel(meta, "schema_v")
        elif damage == "bad_last_seq":
            await redis_client.hset(meta, "last_seq", "six")
        elif damage == "tokens":
            await redis_client.hincrby(meta, "window_tokens", 7)
        elif damage == "order":
            first = await redis_client.lpop(window)
            await redis_client.rpush(window, first)
        elif damage == "summary_step":
            await redis_client.hset(meta, "summary_through_seq", 4)

        loaded = await memory.provider.load_session(
            scope, await state_of(memory, scope, conversation)
        )

        assert loaded.rebuilt_because == reason
        assert shape(loaded.context.recent_turns) == shape(before.recent_turns)  # same window
        healed = await memory.provider.load_session(
            scope, await state_of(memory, scope, conversation)
        )
        assert healed.rebuilt_because is None

    async def test_a_session_that_expired_during_generation_is_rebuilt_not_started_afresh(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str, memory: Memory
    ) -> None:
        """If the keys vanish before the reply is recorded, appending would leave a window that
        holds only the last exchange. It must be rebuilt with the whole history instead."""
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(uow_factory, memory, scope, conversation, PAIRS[:2])
        await redis_client.delete(
            redis_keys.session_window(redis_env, scope.owner_id, conversation),
            redis_keys.session_meta(redis_env, scope.owner_id, conversation),
        )
        last = await exchange(uow_factory, scope, conversation, "Third question", "Third answer")

        result = await memory.provider.record_turn(scope, conversation, last)

        assert result.rebuilt_because == "expired_before_record"
        window = await redis_client.lrange(
            redis_keys.session_window(redis_env, scope.owner_id, conversation), 0, -1
        )
        assert len(window) == 6  # all three exchanges, not just the last one


class TestOnlyFinishedExchangesAreMemory:
    """Found in milestone 11, when a question was first stored before its reply: a rebuild during
    the reply put the question in the window, and recording the finished turn added it again."""

    async def test_a_question_whose_reply_is_still_being_written_is_not_in_a_rebuilt_window(
        self, uow_factory: UowFactory, memory: Memory
    ) -> None:
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(uow_factory, memory, scope, conversation, PAIRS[:1])
        await memory.provider.invalidate(scope, conversation)
        await exchange(uow_factory, scope, conversation, "In flight?", "", complete=False)

        loaded = await memory.provider.load_session(
            scope, await state_of(memory, scope, conversation)
        )

        assert texts(loaded.context.recent_turns) == list(PAIRS[0])

    async def test_recording_the_finished_turn_after_such_a_rebuild_does_not_duplicate_it(
        self, uow_factory: UowFactory, memory: Memory
    ) -> None:
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        pending = await exchange(uow_factory, scope, conversation, "First?", "A1.", complete=False)
        await memory.manager.load(scope, await state_of(memory, scope, conversation))
        async with uow_factory() as uow:  # the reply completes, then is recorded (as finalize does)
            reply = (await uow.messages.recent(scope, conversation, 10))[-1]
            await uow.messages.complete(
                scope, reply.id, content="A1.", status=MessageStatus.COMPLETED
            )
            await uow.commit()
        await memory.manager.record_turn(scope, conversation, pending)

        loaded = await memory.manager.load(scope, await state_of(memory, scope, conversation))

        assert texts(loaded.recent_turns) == ["First?", "A1."]

    async def test_a_failed_exchange_is_not_remembered_but_later_ones_are(
        self, uow_factory: UowFactory, memory: Memory
    ) -> None:
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await exchange(uow_factory, scope, conversation, "Fails?", "", complete=False)
        async with uow_factory() as uow:
            reply = (await uow.messages.recent(scope, conversation, 10))[-1]
            await uow.messages.complete(
                scope, reply.id, content="partial", status=MessageStatus.FAILED
            )
            await uow.commit()
        await converse(uow_factory, memory, scope, conversation, PAIRS[:1])
        await memory.provider.invalidate(scope, conversation)

        loaded = await memory.provider.load_session(
            scope, await state_of(memory, scope, conversation)
        )

        assert texts(loaded.context.recent_turns) == list(PAIRS[0])


class TestOverflowAndSummary:
    def long(self, n: int, size: int = 80) -> str:
        return f"{n:02d} " + "word " * size  # about 100 tokens

    async def test_overflow_produces_a_summary_in_postgresql_and_then_redis(
        self,
        uow_factory: UowFactory,
        redis_client: Redis,
        redis_env: str,
    ) -> None:
        """Spec acceptance: overflow produces a summary update in PostgreSQL."""
        memory = build(uow_factory, redis_client, redis_env, window_tokens=450)
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        pairs = [(self.long(i), self.long(i + 50)) for i in range(1, 6)]  # ~1,000 tokens in all

        await converse(uow_factory, memory, scope, conversation, pairs)

        async with uow_factory() as uow:
            row = await uow.conversations.get(scope, conversation)
            assert row is not None
            assert row.summary and row.summary.startswith("Summary of earlier conversation")
            assert row.summary_through_seq > 0
        meta = await redis_client.hgetall(
            redis_keys.session_meta(redis_env, scope.owner_id, conversation)
        )
        assert meta[b"summary"].decode() == row.summary
        assert int(meta[b"summary_through_seq"]) == row.summary_through_seq
        # the window now holds only what is newer than the summary, and fits its budget
        window = await redis_client.lrange(
            redis_keys.session_window(redis_env, scope.owner_id, conversation), 0, -1
        )
        kept = [SessionTurn.model_validate_json(raw) for raw in window]
        assert all(t.seq > row.summary_through_seq for t in kept)
        assert sum(t.tokens for t in kept) <= 450 or len(kept) <= 2
        assert len(kept) >= 2  # the last exchange is always kept

    async def test_the_summary_is_built_from_the_turns_that_overflowed_and_the_old_summary(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str
    ) -> None:
        memory = build(uow_factory, redis_client, redis_env, window_tokens=450)
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(
            uow_factory,
            memory,
            scope,
            conversation,
            [(self.long(i), self.long(i + 50)) for i in range(1, 8)],
        )

        requests = [r for r in memory.llm.requests if r.route == "summarize"]
        assert len(requests) >= 2
        assert "(none)" in requests[0].messages[0].content  # the first summary has no predecessor
        assert "<previous_summary>\nSummary of earlier" in requests[-1].messages[0].content
        assert all("<turns>" in r.messages[0].content for r in requests)

    async def test_the_loaded_context_carries_the_stored_summary(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str
    ) -> None:
        memory = build(uow_factory, redis_client, redis_env, window_tokens=450)
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(
            uow_factory,
            memory,
            scope,
            conversation,
            [(self.long(i), self.long(i + 50)) for i in range(1, 6)],
        )
        context = await memory.manager.load(scope, await state_of(memory, scope, conversation))
        assert context.summary and context.summary.startswith("Summary of earlier")

    async def test_a_failed_summary_drops_the_session_and_the_next_load_recovers_it(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str
    ) -> None:
        memory = build(uow_factory, redis_client, redis_env, window_tokens=450)
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        memory.llm.fail(LLMUnavailable("down"))
        await converse(
            uow_factory,
            memory,
            scope,
            conversation,
            [(self.long(i), self.long(i + 50)) for i in range(1, 6)],
        )

        async with uow_factory() as uow:
            row = await uow.conversations.get(scope, conversation)
            assert row is not None and row.summary is None  # nothing was stored
        assert not await redis_client.exists(
            redis_keys.session_meta(redis_env, scope.owner_id, conversation)
        )

        # the model is back: loading rebuilds from PostgreSQL, finds the unsummarised turns,
        # and summarises them now
        memory.llm.heal()
        await memory.manager.load(scope, await state_of(memory, scope, conversation))
        await memory.manager.aclose()
        async with uow_factory() as uow:
            row = await uow.conversations.get(scope, conversation)
            assert row is not None and row.summary is not None

    async def test_an_older_summary_can_never_overwrite_a_newer_one(
        self, uow_factory: UowFactory
    ) -> None:
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        async with uow_factory() as uow:
            assert await uow.conversations.advance_summary(scope, conversation, "newer", 10)
            assert not await uow.conversations.advance_summary(scope, conversation, "older", 6)
            assert not await uow.conversations.advance_summary(scope, conversation, "same", 10)
            await uow.commit()
        async with uow_factory() as uow:
            row = await uow.conversations.get(scope, conversation)
            assert row is not None and (row.summary, row.summary_through_seq) == ("newer", 10)

    async def test_a_burst_of_overflows_is_summarised_one_at_a_time_and_in_order(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str
    ) -> None:
        memory = build(uow_factory, redis_client, redis_env, window_tokens=300)
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        running = 0
        most = 0

        def slow(request):  # type: ignore[no-untyped-def]
            nonlocal running, most
            running += 1
            most = max(most, running)
            running -= 1
            return f"summary {len(memory.llm.requests)}"

        memory.llm.script("summarize", slow)
        # record several exchanges without waiting, so summaries overlap
        for i in range(1, 8):
            completed = await exchange(
                uow_factory, scope, conversation, self.long(i), self.long(i + 50)
            )
            await memory.manager.record_turn(scope, conversation, completed)
        await memory.manager.aclose()

        assert most == 1
        async with uow_factory() as uow:
            row = await uow.conversations.get(scope, conversation)
            assert row is not None and row.summary_through_seq > 0

    async def test_the_last_exchange_stays_even_when_it_alone_exceeds_the_budget(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str
    ) -> None:
        memory = build(uow_factory, redis_client, redis_env, window_tokens=50)
        scope = await new_user(uow_factory)
        conversation = await new_conversation(uow_factory, scope)
        await converse(
            uow_factory, memory, scope, conversation, [(self.long(1, 200), self.long(2, 200))]
        )
        context = await memory.manager.load(scope, await state_of(memory, scope, conversation))
        assert len(context.recent_turns) == 2


class TestIsolation:
    async def test_two_users_with_the_same_conversation_id_never_share_a_session(
        self, uow_factory: UowFactory, redis_client: Redis, redis_env: str, memory: Memory
    ) -> None:
        """The key embeds the user id, so even an identical conversation id cannot collide."""
        alice = await new_user(uow_factory, "alice@example.com")
        bob = await new_user(uow_factory, "bob@example.com")
        shared_id = uuid4()
        a_key = redis_keys.session_window(redis_env, alice.owner_id, shared_id)
        b_key = redis_keys.session_window(redis_env, bob.owner_id, shared_id)
        assert a_key != b_key

        await redis_client.rpush(
            a_key, turn(1, "user", "alice's private question").model_dump_json()
        )
        assert await redis_client.llen(b_key) == 0
        assert [
            k.decode()
            async for k in redis_client.scan_iter(match=f"ciq:{redis_env}:sess:{bob.owner_id}:*")
        ] == []

    async def test_one_users_memory_cannot_be_loaded_through_anothers_scope(
        self, uow_factory: UowFactory, memory: Memory
    ) -> None:
        alice = await new_user(uow_factory, "alice@example.com")
        bob = await new_user(uow_factory, "bob@example.com")
        conversation = await new_conversation(uow_factory, alice)
        await converse(uow_factory, memory, alice, conversation)

        # the ownership check comes first: bob has no state for alice's conversation
        assert await memory.reconstructor.state(bob, conversation) is None
        assert await memory.reconstructor.state(alice, conversation) is not None

    async def test_concurrent_sessions_of_different_conversations_stay_separate(
        self, uow_factory: UowFactory, memory: Memory
    ) -> None:
        scope = await new_user(uow_factory)
        first = await new_conversation(uow_factory, scope)
        second = await new_conversation(uow_factory, scope)
        await asyncio.gather(
            converse(uow_factory, memory, scope, first, [("first question", "first answer")]),
            converse(uow_factory, memory, scope, second, [("second question", "second answer")]),
        )
        one = await memory.manager.load(scope, await state_of(memory, scope, first))
        two = await memory.manager.load(scope, await state_of(memory, scope, second))
        assert texts(one.recent_turns) == ["first question", "first answer"]
        assert texts(two.recent_turns) == ["second question", "second answer"]
