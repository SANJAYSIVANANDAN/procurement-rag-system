"""The Redis publisher's coalescing, with a stand-in that only records what was written.

The property that matters: however tokens are merged and whenever the timer fires, what a listener
reads is the same text in the same order, and the stream still obeys the ordering guarantee.
"""

import asyncio
import json
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from redis.exceptions import RedisError

from covenantiq.core.enums import MessageStatus
from covenantiq.streaming.consumer import StoredReply, replay_frames
from covenantiq.streaming.events import check_order, parse_event
from covenantiq.streaming.publisher import RedisStreamPublisher

MID = UUID(int=1)
CID = UUID(int=2)


class RecordingRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.entries: list[tuple[str, dict[str, str]]] = []
        self.expiries: list[tuple[str, int]] = []
        self.fail = fail

    async def xadd(self, key: str, fields: dict[str, str], **options: Any) -> str:
        if self.fail:
            raise RedisError("down")
        self.entries.append((key, fields))
        return f"{len(self.entries)}-0"

    async def expire(self, key: str, seconds: int) -> bool:
        if self.fail:
            raise RedisError("down")
        self.expiries.append((key, seconds))
        return True

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(f["data"]) for _, f in self.entries]


def ev(kind: str, **fields: object) -> Any:
    return parse_event({"event": kind, "message_id": MID, **fields})


def start() -> Any:
    return ev("message_start", conversation_id=CID, seq=2)


def done() -> Any:
    return ev("message_complete", status="completed", grounding_status="grounded", content="x")


def publisher(redis: RecordingRedis, **kw: Any) -> RedisStreamPublisher:
    return RedisStreamPublisher(redis, env="t", **kw)  # type: ignore[arg-type]


class TestCoalescing:
    async def test_small_tokens_are_merged_into_one_write_before_the_next_event(self) -> None:
        redis = RecordingRedis()
        pub = publisher(redis, flush_interval=10, flush_chars=1000)
        await pub.publish(MID, start())
        for i, text in enumerate(["The ", "cap ", "is ", "fees."]):
            await pub.publish(MID, ev("token", text=text, index=i))
        await pub.publish(MID, ev("status", stage="validating"))
        await pub.publish(MID, done())

        kinds = [e["event"] for e in redis.events()]
        assert kinds == ["message_start", "token", "status", "message_complete"]
        merged = redis.events()[1]
        assert merged["text"] == "The cap is fees." and merged["index"] == 0

    async def test_a_batch_is_written_when_it_reaches_the_character_limit(self) -> None:
        redis = RecordingRedis()
        pub = publisher(redis, flush_interval=10, flush_chars=10)
        await pub.publish(MID, start())
        await pub.publish(MID, ev("token", text="12345", index=0))
        assert len(redis.entries) == 1  # only message_start so far
        await pub.publish(MID, ev("token", text="67890", index=1))
        assert [e["event"] for e in redis.events()] == ["message_start", "token"]
        assert redis.events()[1]["text"] == "1234567890"
        await pub.publish(MID, done())

    async def test_a_batch_is_written_after_the_interval_even_if_nothing_else_comes(self) -> None:
        redis = RecordingRedis()
        pub = publisher(redis, flush_interval=0.02, flush_chars=1000)
        await pub.publish(MID, start())
        await pub.publish(MID, ev("token", text="slow", index=0))
        assert len(redis.entries) == 1
        await asyncio.sleep(0.15)
        assert redis.events()[-1]["text"] == "slow"
        await pub.publish(MID, done())
        assert [e["event"] for e in redis.events()] == [
            "message_start",
            "token",
            "message_complete",
        ]

    async def test_the_key_expires_after_the_terminal_event_and_has_a_safety_expiry_before(
        self,
    ) -> None:
        redis = RecordingRedis()
        pub = publisher(redis, ttl_seconds=600)
        await pub.publish(MID, start())
        assert redis.expiries == [(pub.key(MID), 3600)]
        await pub.publish(MID, done())
        assert redis.expiries[-1] == (pub.key(MID), 600)

    async def test_a_redis_failure_is_swallowed_so_the_producer_carries_on(self) -> None:
        pub = publisher(RecordingRedis(fail=True))
        await pub.publish(MID, start())
        await pub.publish(MID, ev("token", text="x" * 100, index=0))
        await pub.publish(MID, done())  # nothing raised


STEP = st.one_of(
    st.tuples(st.just("token"), st.text(alphabet="abc ", min_size=1, max_size=30)),
    st.tuples(st.just("status"), st.sampled_from(["retrieving", "generating", "validating"])),
    st.tuples(st.just("sleep"), st.sampled_from(["0.001", "0.04"])),
)


@settings(max_examples=60, deadline=None)
@given(steps=st.lists(STEP, max_size=40), chars=st.integers(1, 80))
def test_text_and_order_survive_any_pattern_of_tokens_pauses_and_status_events(
    steps: list[tuple[str, str]], chars: int
) -> None:
    async def run() -> RecordingRedis:
        redis = RecordingRedis()
        pub = publisher(redis, flush_interval=0.02, flush_chars=chars)
        await pub.publish(MID, start())
        for i, (kind, value) in enumerate(steps):
            if kind == "token":
                await pub.publish(MID, ev("token", text=value, index=i))
            elif kind == "status":
                await pub.publish(MID, ev("status", stage=value))
            else:
                await asyncio.sleep(float(value))
        await pub.publish(MID, done())
        return redis

    redis = asyncio.run(run())
    written = [parse_event(e) for e in redis.events()]
    check_order(written)

    # tokens and statuses keep their relative order: replay the input, merging adjacent tokens
    expected: list[tuple[str, str]] = []
    for kind, value in steps:
        if kind == "sleep":
            continue
        if kind == "token" and expected and expected[-1][0] == "token":
            expected[-1] = ("token", expected[-1][1] + value)
        else:
            expected.append((kind, value))
    got: list[tuple[str, str]] = []
    for e in redis.events():
        if e["event"] == "token":
            if got and got[-1][0] == "token":
                got[-1] = ("token", got[-1][1] + e["text"])
            else:
                got.append(("token", e["text"]))
        elif e["event"] == "status":
            got.append(("status", e["stage"]))
    assert got == expected


class TestReplayFrames:
    def reply(self, status: MessageStatus, content: str = "Answer [1].") -> StoredReply:
        return StoredReply(
            message_id=MID,
            conversation_id=CID,
            seq=2,
            status=status,
            grounding_status="grounded",
            content=content,
            citations=[
                {
                    "marker": 1,
                    "document_id": str(UUID(int=9)),
                    "document_title": "MSA",
                    "page_start": 1,
                    "page_end": 1,
                    "clause_ref": "6.2",
                    "quote": "q",
                }
            ],
        )

    @pytest.mark.parametrize(
        ("status", "terminal"),
        [
            (MessageStatus.COMPLETED, "message_complete"),
            (MessageStatus.FAILED, "error"),
            (MessageStatus.INTERRUPTED, "error"),
        ],
    )
    def test_a_finished_reply_replays_as_a_valid_stream(
        self, status: MessageStatus, terminal: str
    ) -> None:
        frames = replay_frames(self.reply(status))
        check_order(parse_event(json.loads(f.data)) for f in frames)
        assert frames[-1].event == terminal
        assert [f.id for f in frames] == [f"0-{i}" for i in range(1, len(frames) + 1)]

    def test_a_completed_reply_carries_its_final_text_and_citations(self) -> None:
        frames = replay_frames(self.reply(MessageStatus.COMPLETED))
        assert [f.event for f in frames] == [
            "message_start",
            "token",
            "citation",
            "message_complete",
        ]
        assert json.loads(frames[-1].data)["content"] == "Answer [1]."

    def test_an_empty_reply_has_no_token_event(self) -> None:
        frames = replay_frames(self.reply(MessageStatus.FAILED, content=""))
        assert [f.event for f in frames] == ["message_start", "error"]
