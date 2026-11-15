"""Cross-component chat cases from the spec's critical list (34.3): losing Redis in the middle of a
conversation, and what ends up in the logs. Real PostgreSQL and Redis, a fake model."""

import logging
from collections.abc import Iterator

import pytest
import structlog
from redis.asyncio import Redis

from covenantiq.core import redis_keys
from covenantiq.core.enums import LogFormat, MessageStatus
from covenantiq.core.logging import configure_logging
from tests.integration.chat.stack import Chat, UowFactory, build_chat
from tests.unit.graphs.harness import GOOD_ANSWER, QUESTION

SENTINEL = "ZEBRA-QUOKKA-7741"


@pytest.fixture
async def chat(uow_factory: UowFactory, redis_client: Redis, redis_env: str) -> Chat:
    return await build_chat(uow_factory, redis_client, redis_env)


async def ask(chat: Chat, text: str = QUESTION) -> None:
    await chat.service.submit(chat.principal, chat.conversation_id, content=text)
    await chat.service.idle()


async def flush_redis(redis: Redis, env: str) -> int:
    """What a Redis restart or an eviction does: every key of this environment is gone."""
    keys = [key async for key in redis.scan_iter(match=f"ciq:{env}:*", count=200)]
    if keys:
        await redis.delete(*keys)
    return len(keys)


class TestRedisFlushedMidConversation:
    async def test_the_follow_up_still_sees_the_earlier_turn_and_the_session_comes_back(
        self, chat: Chat, redis_client: Redis, redis_env: str
    ) -> None:
        await ask(chat)
        window = redis_keys.session_window(redis_env, chat.principal.user_id, chat.conversation_id)
        assert await redis_client.exists(window)

        assert await flush_redis(redis_client, redis_env) > 0
        assert not await redis_client.exists(window)

        await ask(chat, "And what about the exclusions?")

        answers = [r for r in chat.llm.requests if r.route == "answer"]
        assert [m.role for m in answers[-1].messages] == ["user", "assistant", "user"]
        assert answers[-1].messages[0].content == QUESTION  # rebuilt from PostgreSQL
        assert answers[-1].messages[1].content == GOOD_ANSWER
        assert await redis_client.exists(window)  # and the window is back for the next one
        replies = [m for m in await chat.messages() if m.role.value == "assistant"]
        assert [m.status for m in replies] == [MessageStatus.COMPLETED] * 2

    async def test_the_lock_is_not_needed_to_survive_a_flush_so_the_conversation_stays_usable(
        self, chat: Chat, redis_client: Redis, redis_env: str
    ) -> None:
        await ask(chat)
        await flush_redis(redis_client, redis_env)
        await ask(chat, "Anything else?")
        assert not await chat.lock_held()
        assert (await chat.conversation()).last_completed_seq == 4


@pytest.fixture(params=[LogFormat.JSON, LogFormat.CONSOLE])
def app_logging(request: pytest.FixtureRequest) -> Iterator[None]:
    """The application's own log configuration, which tests otherwise do not use."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    configure_logging("DEBUG", request.param)
    yield
    structlog.reset_defaults()
    root.handlers, root.level = handlers, level


@pytest.mark.usefixtures("app_logging")
class TestLogsHoldNoDocumentText:
    async def test_a_reply_and_a_failed_reply_log_nothing_from_the_contract(
        self, chat: Chat, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        passage = f"Clause {SENTINEL}: the Supplier shall pay the fees paid in twelve months."
        chat.retriever.set_passage(passage)
        chat.llm.script(
            "answer", lambda r: f"The Supplier shall pay the fees paid. [C1] {SENTINEL}"
        )
        caplog.set_level(logging.DEBUG)

        await ask(chat, "What must the Supplier pay?")  # a validated answer
        chat.llm.fail_stream_after(3)
        await ask(chat, "And the exclusions?")  # a model failure after tokens

        seen = capsys.readouterr()
        everything = "\n".join([caplog.text, seen.out, seen.err])
        assert "chat.run_finished" in everything or "chat" in caplog.text  # logging happened
        assert SENTINEL not in everything
        assert "Supplier shall pay" not in everything

    async def test_the_question_is_not_logged_either(
        self, chat: Chat, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        caplog.set_level(logging.DEBUG)
        await ask(chat, f"Does {SENTINEL} apply to me?")
        seen = capsys.readouterr()
        assert SENTINEL not in "\n".join([caplog.text, seen.out, seen.err])
