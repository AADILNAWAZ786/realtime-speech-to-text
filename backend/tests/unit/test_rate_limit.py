from __future__ import annotations

import pytest

from app.core.exceptions import RateLimitedError
from app.core.rate_limit import LoginRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _limiter(clock: FakeClock, **kwargs: object) -> LoginRateLimiter:
    options: dict[str, object] = {"max_attempts": 3, "window_seconds": 60, "clock": clock}
    options.update(kwargs)
    return LoginRateLimiter(**options)  # type: ignore[arg-type]


def test_blocks_after_max_failures_and_reports_retry_after() -> None:
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(3):
        limiter.check("k")
        limiter.record_failure("k")
    with pytest.raises(RateLimitedError) as exc_info:
        limiter.check("k")
    assert exc_info.value.headers == {"Retry-After": "61"}


def test_window_slides() -> None:
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(3):
        limiter.record_failure("k")
    clock.now += 60
    limiter.check("k")  # all failures have aged out


def test_reset_clears_failures() -> None:
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(3):
        limiter.record_failure("k")
    limiter.reset("k")
    limiter.check("k")


def test_keys_are_independent() -> None:
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(3):
        limiter.record_failure("a")
    limiter.check("b")


def test_disabled_limiter_never_blocks() -> None:
    clock = FakeClock()
    limiter = _limiter(clock, enabled=False)
    for _ in range(10):
        limiter.record_failure("k")
    limiter.check("k")


def test_memory_is_bounded() -> None:
    clock = FakeClock()
    limiter = _limiter(clock, max_tracked_keys=2)
    for key in ("a", "b", "c"):
        for _ in range(3):
            limiter.record_failure(key)
    limiter.check("a")  # oldest key was evicted
    with pytest.raises(RateLimitedError):
        limiter.check("c")
