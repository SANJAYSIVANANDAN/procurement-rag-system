import time
from collections.abc import Callable

from covenantiq.core.logging import get_logger

log = get_logger(__name__)

FAILURES_BEFORE_OPENING = 3
OPEN_FOR_SECONDS = 30.0


class CircuitBreaker:
    """Stops calling a dependency that keeps failing.

    A reranker that is down or slow must not slow every request with it: after three failures in a
    row the breaker opens and calls fail at once for 30 seconds, and the caller keeps the fused
    order (spec 19.8). A success resets the count.
    """

    def __init__(
        self,
        *,
        failures: int = FAILURES_BEFORE_OPENING,
        open_for_seconds: float = OPEN_FOR_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        name: str = "rerank",
    ) -> None:
        self._failures_to_open = failures
        self._open_for = open_for_seconds
        self._clock = clock
        self._name = name
        self._failures = 0
        self._open_until = 0.0

    def is_open(self) -> bool:
        return self._clock() < self._open_until

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._failures_to_open:
            self._open_until = self._clock() + self._open_for
            self._failures = 0
            log.warning(f"{self._name}.skipping", seconds=self._open_for)

    def record_success(self) -> None:
        self._failures = 0
