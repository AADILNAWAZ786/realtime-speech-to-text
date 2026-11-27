"""Inference scheduler: runs blocking Whisper calls in a thread pool with priorities.

Rules:
- At most `max_concurrency` jobs run at once. CPU budget: max_concurrency x the engine's
  cpu_threads <= vCPUs - 1 (see recommended_concurrency / validate_cpu_budget, which raise
  when the budget cannot be met rather than silently exceeding it).
- FINAL jobs always start before PARTIAL jobs and are never dropped. Closing the scheduler
  waits for every queued final to finish.
- A session never has two finals running at once (finals of one session run in order).
- Each session has at most ONE queued partial: a newer request replaces it (the replaced
  future resolves to None). A queued partial whose `is_stale()` returns True when it would
  start is skipped (resolves to None). Partials are served round-robin across sessions.

All bookkeeping happens on the event-loop thread; only the job callables run in workers.
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import logging
import os
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class JobKind(StrEnum):
    FINAL = "final"
    PARTIAL = "partial"


class SchedulerClosedError(RuntimeError):
    pass


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


def _cpu_count(cpu_count: int | None) -> int:
    if cpu_count is None:
        detected = os.cpu_count()
        if detected is None:
            raise ValueError("could not detect the number of CPUs; pass cpu_count explicitly")
        return detected
    return _positive_int("cpu_count", cpu_count)


def recommended_concurrency(
    cpu_count: int | None = None,
    threads_per_job: int = 1,
    *,
    allow_oversubscription: bool = False,
) -> int:
    """Largest number of concurrent inference jobs within the CPU budget:

        jobs x threads_per_job <= cpu_count - 1   (one core reserved for the event loop)

    Raises ValueError when not even one job fits (e.g. 1 vCPU, or threads_per_job larger
    than the spare cores), instead of silently returning a value that breaks the budget.
    With `allow_oversubscription=True` it returns 1 and logs a warning: an explicit choice
    to share the event-loop core with inference.
    """
    cpus = _cpu_count(cpu_count)
    threads = _positive_int("threads_per_job", threads_per_job)
    spare = cpus - 1
    jobs = spare // threads
    if jobs >= 1:
        return jobs
    message = (
        f"CPU budget cannot be met: {cpus} vCPU(s) leave {spare} core(s) after reserving "
        f"one for the event loop, but one inference job uses {threads} thread(s)"
    )
    if allow_oversubscription:
        logger.warning("%s; running 1 job anyway (oversubscribed)", message)
        return 1
    hint = f"use threads_per_job <= {spare}" if spare >= 1 else "use a machine with >= 2 vCPUs"
    raise ValueError(f"{message}: {hint}, or pass allow_oversubscription=True")


def validate_cpu_budget(
    max_concurrency: int, threads_per_job: int, cpu_count: int | None = None
) -> None:
    """Raise ValueError if max_concurrency x threads_per_job exceeds cpu_count - 1."""
    cpus = _cpu_count(cpu_count)
    jobs = _positive_int("max_concurrency", max_concurrency)
    threads = _positive_int("threads_per_job", threads_per_job)
    if jobs * threads > cpus - 1:
        raise ValueError(
            f"{jobs} job(s) x {threads} thread(s) = {jobs * threads} exceeds the budget of "
            f"{cpus - 1} core(s) ({cpus} vCPU(s) minus one for the event loop)"
        )


@dataclass(eq=False)
class _Job[T]:
    kind: JobKind
    session_id: str
    fn: Callable[[], T]
    future: asyncio.Future[T | None]
    order: int
    is_stale: Callable[[], bool] | None = None


@dataclass
class SchedulerStats:
    finals_completed: int = 0
    finals_failed: int = 0
    partials_completed: int = 0
    partials_failed: int = 0
    partials_replaced: int = 0
    partials_stale: int = 0
    partials_cancelled: int = 0
    max_observed_concurrency: int = 0
    queue_wait_ms: deque[float] = field(default_factory=lambda: deque(maxlen=1000))


class InferenceScheduler:
    def __init__(self, max_concurrency: int = 1, *, thread_name_prefix: str = "stt-infer") -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.max_concurrency = max_concurrency
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrency, thread_name_prefix=thread_name_prefix
        )
        self._finals: deque[_Job[Any]] = deque()
        self._partials: OrderedDict[str, _Job[Any]] = OrderedDict()
        self._sessions_running_final: set[str] = set()
        self._running = 0
        self._order = itertools.count()
        self._closed = False
        self._idle = asyncio.Event()
        self._idle.set()
        self._submitted_at: dict[int, float] = {}
        self.stats = SchedulerStats()

    # ----- submission -----

    def submit_final(self, session_id: str, fn: Callable[[], T]) -> asyncio.Future[T | None]:
        """Queue a final job. Its future always completes with the result or the error."""
        self._ensure_open()
        job = self._new_job(JobKind.FINAL, session_id, fn)
        self._finals.append(job)
        self._dispatch()
        return job.future

    def submit_partial(
        self,
        session_id: str,
        fn: Callable[[], T],
        *,
        is_stale: Callable[[], bool] | None = None,
    ) -> asyncio.Future[T | None]:
        """Queue a partial job, replacing this session's queued (not running) partial.

        The future resolves to None if the job is replaced, stale or cancelled.
        """
        self._ensure_open()
        job = self._new_job(JobKind.PARTIAL, session_id, fn, is_stale)
        previous = self._partials.get(session_id)
        if previous is not None:
            self.stats.partials_replaced += 1
            self._drop_queued(previous)
        self._partials[session_id] = job  # keeps the session's round-robin position
        # A caller cancelling a *queued* partial removes it right away (not at next dispatch).
        job.future.add_done_callback(functools.partial(self._on_partial_future_done, job))
        self._dispatch()
        return job.future

    def cancel_partials(self, session_id: str) -> None:
        job = self._partials.pop(session_id, None)
        if job is not None:
            self.stats.partials_cancelled += 1
            self._drop_queued(job)
            self._update_idle()

    # ----- introspection -----

    @property
    def running(self) -> int:
        return self._running

    @property
    def queued_finals(self) -> int:
        return len(self._finals)

    @property
    def queued_partials(self) -> int:
        return len(self._partials)

    @property
    def tracked_submissions(self) -> int:
        """Submission timestamps held; always equals the number of queued jobs."""
        return len(self._submitted_at)

    async def wait_idle(self) -> None:
        """Wait until nothing is queued or running."""
        await self._idle.wait()

    async def aclose(self) -> None:
        """Stop accepting work, drop queued partials, finish every final, then shut down."""
        if self._closed:
            return
        self._closed = True
        for session_id in list(self._partials):
            self.cancel_partials(session_id)
        await self.wait_idle()
        self._executor.shutdown(wait=True)

    # ----- internals -----

    def _ensure_open(self) -> None:
        if self._closed:
            raise SchedulerClosedError("scheduler is closed")

    def _new_job(
        self,
        kind: JobKind,
        session_id: str,
        fn: Callable[[], T],
        is_stale: Callable[[], bool] | None = None,
    ) -> _Job[T]:
        loop = asyncio.get_running_loop()
        job: _Job[T] = _Job(kind, session_id, fn, loop.create_future(), next(self._order), is_stale)
        # Keyed by the job's unique sequence number (id() values can be reused after GC).
        self._submitted_at[job.order] = loop.time()
        self._idle.clear()
        return job

    @staticmethod
    def _resolve_skipped(job: _Job[Any]) -> None:
        if not job.future.done():
            job.future.set_result(None)

    def _forget(self, job: _Job[Any]) -> float | None:
        """Drop the job's submission timestamp. Called on every path that removes a job
        from the queue: dispatched, replaced, cancelled, stale or dropped at shutdown."""
        return self._submitted_at.pop(job.order, None)

    def _drop_queued(self, job: _Job[Any]) -> None:
        self._forget(job)
        self._resolve_skipped(job)

    def _on_partial_future_done(self, job: _Job[Any], future: asyncio.Future[Any]) -> None:
        if future.cancelled() and self._partials.get(job.session_id) is job:
            del self._partials[job.session_id]
            self._forget(job)
            self.stats.partials_cancelled += 1
            self._update_idle()

    def _next_job(self) -> _Job[Any] | None:
        for job in self._finals:
            if job.session_id not in self._sessions_running_final:
                self._finals.remove(job)
                return job
        while self._partials:
            _, job = self._partials.popitem(last=False)
            if job.future.done():  # cancelled by the caller
                self.stats.partials_cancelled += 1
                self._forget(job)
                continue
            if job.is_stale is not None and job.is_stale():
                self.stats.partials_stale += 1
                self._drop_queued(job)
                continue
            return job
        return None

    def _dispatch(self) -> None:
        loop = asyncio.get_running_loop()
        while self._running < self.max_concurrency:
            job = self._next_job()
            if job is None:
                break
            if job.kind is JobKind.FINAL:
                self._sessions_running_final.add(job.session_id)
            self._running += 1
            self.stats.max_observed_concurrency = max(
                self.stats.max_observed_concurrency, self._running
            )
            submitted = self._forget(job)
            if submitted is not None:
                self.stats.queue_wait_ms.append((loop.time() - submitted) * 1000)
            work = loop.run_in_executor(self._executor, job.fn)
            work.add_done_callback(functools.partial(self._on_done, job))
        self._update_idle()

    def _on_done(self, job: _Job[Any], done: asyncio.Future[Any]) -> None:
        self._running -= 1
        if job.kind is JobKind.FINAL:
            self._sessions_running_final.discard(job.session_id)
        error: BaseException | None = (
            asyncio.CancelledError("executor job cancelled")
            if done.cancelled()
            else done.exception()
        )
        final = job.kind is JobKind.FINAL
        if error is not None:
            if final:
                self.stats.finals_failed += 1
            else:
                self.stats.partials_failed += 1
            if not job.future.done():
                job.future.set_exception(error)
        else:
            if final:
                self.stats.finals_completed += 1
            else:
                self.stats.partials_completed += 1
            if not job.future.done():
                job.future.set_result(done.result())
        self._dispatch()

    def _update_idle(self) -> None:
        if self._running == 0 and not self._finals and not self._partials:
            self._idle.set()
        else:
            self._idle.clear()
