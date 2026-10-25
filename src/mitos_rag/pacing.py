"""Pacing to a per-minute Bedrock quota.

Bedrock quotas are per model, so two callers of the same model (the Haiku reranker
and Haiku answers) must share one `Pacer`, or together they go over the quota.
"""

import time
from collections.abc import Callable


class Pacer:
    def __init__(
        self,
        requests_per_minute: int,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval = 60.0 / requests_per_minute + 0.5
        self.sleep = sleep
        self.clock = clock
        self._last: float | None = None

    def wait(self) -> None:
        """Sleep until a fresh request fits the quota, then mark it as sent."""
        if self._last is not None:
            wait = self.min_interval - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()
