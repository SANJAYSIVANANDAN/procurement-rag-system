"""The production container wires the chat stack from settings, with no network needed to build."""

from covenantiq.api.container import build_container
from covenantiq.core.config import Settings
from covenantiq.services.chat_service import ChatService


async def test_the_container_builds_the_chat_stack_offline(valid_env: dict[str, str]) -> None:
    container = build_container(Settings(_env_file=None))  # type: ignore[call-arg]
    try:
        assert isinstance(container.chat_service, ChatService)
        assert container.memory is not None and container.retrieval is not None
    finally:
        await container.aclose()  # nothing in flight: returns at once
