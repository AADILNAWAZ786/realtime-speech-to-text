"""InferenceScheduler: priorities, concurrency limits, stale/replaced partials, draining."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

import pytest

from app.realtime.scheduler import (
    InferenceScheduler,
    SchedulerClosedError,
    recommended_concurrency,
    validate_cpu_budget,
)


class Gate:
    """Blocks worker threads until released, and records execution order/concurrency."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.order: list[str] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def job(self, name: str, *, block: bool = True, duration: float = 0.0) -> Callable[[], str]:
        def run() -> str:
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.order.append(name)
            try:
                if block:
                    self.release.wait(timeout=5)
                time.sleep(duration)
                return name
            finally:
                with self._lock:
                    self.active -= 1

        return run


async def _until(predicate: Callable[[], bool]) -> None:
    for _ in range(400):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timeout")


async def test_finals_run_before_partials() -> None:
    gate = Gate()
    scheduler = InferenceScheduler(1)
    blocker = scheduler.submit_final("s0", gate.job("blocker"))
    await _until(lambda: scheduler.running == 1)
    partials = [
        scheduler.submit_partial(f"p{i}", gate.job(f"partial-{i}", block=False)) for i in range(3)
    ]
    finals = [
        scheduler.submit_final(f"f{i}", gate.job(f"final-{i}", block=False)) for i in range(2)
    ]
    gate.release.set()
    await asyncio.gather(blocker, *partials, *finals)
    assert gate.order == ["blocker", "final-0", "final-1", "partial-0", "partial-1", "partial-2"]
    await scheduler.aclose()


async def test_concurrency_limit_is_respected() -> None:
    gate = Gate()
    scheduler = InferenceScheduler(2)
    futures = [
        scheduler.submit_final(f"s{i}", gate.job(f"j{i}", block=False, duration=0.03))
        for i in range(8)
    ]
    await asyncio.gather(*futures)
    assert gate.max_active == 2
    assert scheduler.stats.max_observed_concurrency == 2
    assert scheduler.stats.finals_completed == 8
    await scheduler.aclose()


async def test_one_final_at_a_time_per_session() -> None:
    gate = Gate()
    scheduler = InferenceScheduler(3)
    a1 = scheduler.submit_final("a", gate.job("a1"))
    a2 = scheduler.submit_final("a", gate.job("a2"))
    b1 = scheduler.submit_final("b", gate.job("b1"))
    await _until(lambda: scheduler.running == 2)
    await asyncio.sleep(0.05)
    assert sorted(gate.order) == ["a1", "b1"]  # a2 waits although a slot is free
    gate.release.set()
    await asyncio.gather(a1, a2, b1)
    assert gate.order.index("a2") > gate.order.index("a1")
    await scheduler.aclose()


async def test_newer_partial_replaces_queued_one() -> None:
    gate = Gate()
    scheduler = InferenceScheduler(1)
    blocker = scheduler.submit_final("x", gate.job("blocker"))
    await _until(lambda: scheduler.running == 1)
    old = scheduler.submit_partial("s", gate.job("old", block=False))
    new = scheduler.submit_partial("s", gate.job("new", block=False))
    assert old.done() and old.result() is None  # replaced immediately
    gate.release.set()
    assert await new == "new"
    await blocker
    assert "old" not in gate.order
    assert scheduler.stats.partials_replaced == 1
    await scheduler.aclose()


async def test_stale_partial_is_skipped() -> None:
    gate = Gate()
    scheduler = InferenceScheduler(1)
    blocker = scheduler.submit_final("x", gate.job("blocker"))
    await _until(lambda: scheduler.running == 1)
    stale = {"value": False}
    partial = scheduler.submit_partial(
        "s", gate.job("partial", block=False), is_stale=lambda: stale["value"]
    )
    stale["value"] = True  # e.g. the utterance ended while the job was queued
    gate.release.set()
    assert await partial is None
    await blocker
    assert gate.order == ["blocker"]
    assert scheduler.stats.partials_stale == 1
    await scheduler.aclose()


async def test_partials_round_robin_across_sessions() -> None:
    gate = Gate()
    scheduler = InferenceScheduler(1)
    blocker = scheduler.submit_final("x", gate.job("blocker"))
    await _until(lambda: scheduler.running == 1)
    futures = [scheduler.submit_partial(s, gate.job(s, block=False)) for s in ("a", "b", "c")]
    futures.append(scheduler.submit_partial("a", gate.job("a2", block=False)))  # replaces a
    gate.release.set()
    await asyncio.gather(blocker, *futures)
    assert gate.order == ["blocker", "a2", "b", "c"]  # a keeps its turn
    await scheduler.aclose()


async def test_errors_propagate_to_the_caller() -> None:
    scheduler = InferenceScheduler(1)

    def boom() -> None:
        raise RuntimeError("decoder crashed")

    with pytest.raises(RuntimeError, match="decoder crashed"):
        await scheduler.submit_final("s", boom)
    with pytest.raises(RuntimeError):
        await scheduler.submit_partial("s", boom)
    assert scheduler.stats.finals_failed == scheduler.stats.partials_failed == 1
    await scheduler.aclose()


async def test_inference_runs_outside_the_event_loop_thread() -> None:
    scheduler = InferenceScheduler(1)
    loop_thread = threading.get_ident()
    worker_thread = await scheduler.submit_final("s", threading.get_ident)
    assert worker_thread != loop_thread
    # The loop stays responsive while a slow job runs.
    slow = scheduler.submit_final("s", lambda: time.sleep(0.2))
    started = time.monotonic()
    await asyncio.sleep(0.01)
    assert time.monotonic() - started < 0.1
    await slow
    await scheduler.aclose()


async def test_close_finishes_finals_and_drops_partials() -> None:
    gate = Gate()
    scheduler = InferenceScheduler(1)
    blocker = scheduler.submit_final("x", gate.job("blocker"))
    await _until(lambda: scheduler.running == 1)
    final = scheduler.submit_final("y", gate.job("final", block=False))
    partial = scheduler.submit_partial("z", gate.job("partial", block=False))
    closing = asyncio.create_task(scheduler.aclose())
    await asyncio.sleep(0.02)
    assert partial.done() and partial.result() is None
    gate.release.set()
    await closing
    assert await final == "final" and await blocker == "blocker"
    assert gate.order == ["blocker", "final"]
    with pytest.raises(SchedulerClosedError):
        scheduler.submit_final("s", lambda: None)


async def test_cancelled_partial_future_is_not_run() -> None:
    gate = Gate()
    scheduler = InferenceScheduler(1)
    blocker = scheduler.submit_final("x", gate.job("blocker"))
    await _until(lambda: scheduler.running == 1)
    partial = scheduler.submit_partial("s", gate.job("partial", block=False))
    partial.cancel()
    gate.release.set()
    await blocker
    await scheduler.wait_idle()
    assert gate.order == ["blocker"]
    await scheduler.aclose()


# ---------------------------------------------------------------- CPU budget


@pytest.mark.parametrize(
    ("cpus", "threads", "expected"),
    [(2, 1, 1), (4, 1, 3), (8, 3, 2), (8, 7, 1), (16, 4, 3), (3, 2, 1)],
)
def test_recommended_concurrency_reserves_one_core(cpus: int, threads: int, expected: int) -> None:
    jobs = recommended_concurrency(cpus, threads)
    assert jobs == expected
    assert jobs * threads <= cpus - 1  # the documented budget really holds


@pytest.mark.parametrize(("cpus", "threads"), [(1, 1), (2, 2), (4, 4), (1, 4)])
def test_recommended_concurrency_raises_when_budget_cannot_be_met(cpus: int, threads: int) -> None:
    with pytest.raises(ValueError, match="CPU budget cannot be met"):
        recommended_concurrency(cpus, threads)


def test_oversubscription_is_explicit_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        assert recommended_concurrency(1, 1, allow_oversubscription=True) == 1
    assert "oversubscribed" in caplog.text


@pytest.mark.parametrize(
    ("cpus", "threads"), [(0, 1), (-2, 1), (4, 0), (4, -1), (True, 1), (4, 1.5), ("4", 1)]
)
def test_recommended_concurrency_validates_inputs(cpus: object, threads: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        recommended_concurrency(cpus, threads)  # type: ignore[arg-type]


def test_recommended_concurrency_detects_cpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 6)
    assert recommended_concurrency() == 5
    monkeypatch.setattr("os.cpu_count", lambda: None)
    with pytest.raises(ValueError, match="could not detect"):
        recommended_concurrency()


def test_validate_cpu_budget() -> None:
    validate_cpu_budget(3, 1, cpu_count=4)
    validate_cpu_budget(1, 3, cpu_count=4)
    with pytest.raises(ValueError, match="exceeds the budget"):
        validate_cpu_budget(2, 2, cpu_count=4)
    with pytest.raises(ValueError, match="exceeds the budget"):
        validate_cpu_budget(1, 1, cpu_count=1)
    with pytest.raises(ValueError, match="positive integer"):
        validate_cpu_budget(0, 1, cpu_count=4)


def test_scheduler_rejects_zero_concurrency() -> None:
    with pytest.raises(ValueError):
        InferenceScheduler(0)


# ---------------------------------------------------------------- submission bookkeeping


def assert_tracking_matches_queue(scheduler: InferenceScheduler) -> None:
    """Timestamps exist for exactly the queued jobs - nothing discarded lingers."""
    queued = {job.order for job in scheduler._finals} | {
        job.order for job in scheduler._partials.values()
    }
    assert set(scheduler._submitted_at) == queued
    assert scheduler.tracked_submissions == scheduler.queued_finals + scheduler.queued_partials


async def _blocked(gate: Gate) -> tuple[InferenceScheduler, asyncio.Future[str | None]]:
    scheduler = InferenceScheduler(1)
    blocker = scheduler.submit_final("blocker", gate.job("blocker"))
    await _until(lambda: scheduler.running == 1)
    assert scheduler._submitted_at == {}  # the running job's entry was removed at dispatch
    return scheduler, blocker


async def test_replacing_hundreds_of_partials_keeps_only_the_latest_timestamp() -> None:
    gate = Gate()
    scheduler, blocker = await _blocked(gate)
    futures = [scheduler.submit_partial("s", gate.job(f"p{i}", block=False)) for i in range(500)]
    for session in range(50):  # and many sessions, each replaced repeatedly
        for _ in range(10):
            futures.append(scheduler.submit_partial(f"s{session}", gate.job("x", block=False)))
    assert_tracking_matches_queue(scheduler)
    assert scheduler.tracked_submissions == 51
    assert scheduler.stats.partials_replaced == 499 + 50 * 9
    replaced = [f for f in futures if f.done()]
    assert len(replaced) == 499 + 50 * 9 and all(f.result() is None for f in replaced)
    gate.release.set()
    await blocker
    await scheduler.wait_idle()
    assert scheduler._submitted_at == {}
    await scheduler.aclose()


async def test_cancelled_queued_partials_release_timestamps_immediately() -> None:
    gate = Gate()
    scheduler, blocker = await _blocked(gate)
    futures = [scheduler.submit_partial(f"s{i}", gate.job("x", block=False)) for i in range(300)]
    for future in futures[:200]:
        future.cancel()  # caller-side cancellation
    await asyncio.sleep(0)  # done-callbacks run on the next loop iteration
    assert scheduler.queued_partials == 100
    assert_tracking_matches_queue(scheduler)
    for i in range(200, 300):
        scheduler.cancel_partials(f"s{i}")  # scheduler-side cancellation
    assert scheduler._submitted_at == {}
    assert scheduler.stats.partials_cancelled == 300
    gate.release.set()
    await blocker
    await scheduler.wait_idle()
    assert gate.order == ["blocker"]
    await scheduler.aclose()


async def test_stale_partials_release_timestamps() -> None:
    gate = Gate()
    scheduler, blocker = await _blocked(gate)
    stale = {"value": False}
    futures = [
        scheduler.submit_partial(
            f"s{i}", gate.job("x", block=False), is_stale=lambda: stale["value"]
        )
        for i in range(200)
    ]
    assert scheduler.tracked_submissions == 200
    stale["value"] = True
    gate.release.set()
    await blocker
    await scheduler.wait_idle()
    assert all(f.result() is None for f in futures)
    assert scheduler.stats.partials_stale == 200
    assert scheduler._submitted_at == {}
    await scheduler.aclose()


async def test_close_with_pending_partials_releases_timestamps() -> None:
    gate = Gate()
    scheduler, blocker = await _blocked(gate)
    partials = [scheduler.submit_partial(f"s{i}", gate.job("p", block=False)) for i in range(150)]
    final = scheduler.submit_final("f", gate.job("final", block=False))
    closing = asyncio.create_task(scheduler.aclose())
    await asyncio.sleep(0.02)
    assert all(p.done() and p.result() is None for p in partials)
    assert_tracking_matches_queue(scheduler)  # only the queued final is still tracked
    assert scheduler.tracked_submissions == 1
    gate.release.set()
    await closing
    assert await final == "final" and await blocker == "blocker"
    assert scheduler._submitted_at == {}


async def test_completed_and_failed_jobs_release_timestamps() -> None:
    scheduler = InferenceScheduler(2)

    def boom() -> None:
        raise RuntimeError("x")

    ok = [scheduler.submit_final(f"s{i}", lambda: 1) for i in range(100)]
    bad = [scheduler.submit_final(f"b{i}", boom) for i in range(20)]
    await asyncio.gather(*ok, *bad, return_exceptions=True)
    await scheduler.wait_idle()
    assert scheduler._submitted_at == {}
    assert len(scheduler.stats.queue_wait_ms) == 120
    await scheduler.aclose()


async def test_queue_wait_stats_are_bounded() -> None:
    scheduler = InferenceScheduler(1)
    await asyncio.gather(*(scheduler.submit_final("s", lambda: None) for _ in range(1500)))
    assert len(scheduler.stats.queue_wait_ms) == 1000
    await scheduler.aclose()
