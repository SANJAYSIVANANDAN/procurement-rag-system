"""Where a chat reply's events go (spec 26.3).

The producer publishes through this interface and does not know who is listening. Milestone 11
ships an in-memory implementation (tests, and a single-process run); the Redis Streams publisher
and the consumer that serves the events to browsers arrive with the SSE endpoint.
"""

from collections import defaultdict
from typing import Protocol
from uuid import UUID

from covenantiq.streaming.events import SSEEvent


class StreamPublisher(Protocol):
    async def publish(self, message_id: UUID, event: SSEEvent) -> None: ...


class NullPublisher:
    async def publish(self, message_id: UUID, event: SSEEvent) -> None:
        return None


class InMemoryPublisher:
    """Keeps every event, per message, in order."""

    def __init__(self) -> None:
        self.events: dict[UUID, list[SSEEvent]] = defaultdict(list)

    async def publish(self, message_id: UUID, event: SSEEvent) -> None:
        self.events[message_id].append(event)

    def of(self, message_id: UUID) -> list[SSEEvent]:
        return list(self.events[message_id])

    def kinds(self, message_id: UUID) -> list[str]:
        return [e.event for e in self.events[message_id]]
