"""In-memory sliding-window limiter for failed login attempts.

Scope and limits: state lives in one process, which matches the single-worker deployment
described in the architecture. A multi-instance deployment would need a shared store
(e.g. Redis) instead.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable

from app.core.exceptions import RateLimitedError


class LoginRateLimiter:
    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int,
        enabled: bool = True,
        max_tracked_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_attempts = max_attempts
        self._window = float(window_seconds)
        self._enabled = enabled
        self._max_keys = max_tracked_keys
        self._clock = clock
        # key -> timestamps of recent failures; ordered by last use for bounded memory.
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def _prune(self, key: str, now: float) -> deque[float] | None:
        attempts = self._failures.get(key)
        if attempts is None:
            return None
        while attempts and now - attempts[0] >= self._window:
            attempts.popleft()
        if not attempts:
            del self._failures[key]
            return None
        return attempts

    def check(self, key: str) -> None:
        """Raise RateLimitedError if `key` has used up its failed attempts."""
        if not self._enabled:
            return
        now = self._clock()
        attempts = self._prune(key, now)
        if attempts is not None and len(attempts) >= self._max_attempts:
            retry_after = max(1, int(self._window - (now - attempts[0])) + 1)
            raise RateLimitedError(retry_after_seconds=retry_after)

    def record_failure(self, key: str) -> None:
        if not self._enabled:
            return
        now = self._clock()
        attempts = self._prune(key, now) or deque()
        attempts.append(now)
        self._failures[key] = attempts
        self._failures.move_to_end(key)
        while len(self._failures) > self._max_keys:
            self._failures.popitem(last=False)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
