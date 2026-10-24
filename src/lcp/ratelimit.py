"""Token-bucket rate limiting used for application back-pressure (``BUSY``, spec 5.2)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Per-connection limit: ``rate`` requests/second with bursts of up to ``burst``."""

    rate: float
    burst: float | None = None


class TokenBucket:
    """Classic token bucket.  ``try_acquire`` returns ``0.0`` on success, otherwise the
    number of seconds until enough tokens will be available."""

    def __init__(
        self,
        rate: float,
        burst: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        capacity = max(1.0, rate) if burst is None else float(burst)
        if capacity < 1.0:
            raise ValueError("burst must be at least 1")
        self.rate = float(rate)
        self.capacity = capacity
        self._tokens = capacity
        self._clock = clock
        self._updated = clock()

    def try_acquire(self, tokens: float = 1.0) -> float:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        if self._tokens >= tokens:
            self._tokens -= tokens
            return 0.0
        return (tokens - self._tokens) / self.rate
