"""Per-connection audio rate limiting and the process-wide recording registry."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Protocol

from app.ws.protocol import CloseCode, ErrorCode, ProtocolError

SAMPLE_RATE = 16_000


class AudioRateLimiter:
    """Token bucket in audio samples.

    A client may run `burst_seconds` of audio ahead of real time; beyond that it may send at
    most `factor` x real time. Prevents a client from flooding the shared inference queue.
    """

    def __init__(
        self,
        *,
        factor: float,
        burst_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = factor * SAMPLE_RATE  # samples per wall-clock second
        self._capacity = burst_seconds * SAMPLE_RATE
        self._tokens = self._capacity
        self._clock = clock
        self._last = clock()

    def allow(self, samples: int) -> bool:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if samples > self._tokens:
            return False
        self._tokens -= samples
        return True


class Stoppable(Protocol):
    def request_shutdown(self) -> None: ...

    async def wait_closed(self) -> None: ...


class RecordingLease:
    """A held recording slot. Release exactly once (idempotent)."""

    def __init__(self, registry: RecordingRegistry, user_id: uuid.UUID) -> None:
        self._registry = registry
        self.user_id = user_id
        self.connection: Stoppable | None = None
        self.released = False

    def attach(self, connection: Stoppable) -> None:
        self.connection = connection

    def release(self) -> None:
        if not self.released:
            self.released = True
            self._registry._release(self)


class RecordingRegistry:
    """One active recording per user and a global limit. Event-loop only (no locking)."""

    def __init__(self, max_sessions: int) -> None:
        self.max_sessions = max_sessions
        self._leases: dict[uuid.UUID, RecordingLease] = {}
        self._closing = False

    @property
    def active_count(self) -> int:
        return len(self._leases)

    def is_active(self, user_id: uuid.UUID) -> bool:
        return user_id in self._leases

    def acquire(self, user_id: uuid.UUID) -> RecordingLease:
        if self._closing:
            raise ProtocolError(ErrorCode.SERVER_SHUTDOWN, CloseCode.SERVICE_RESTART)
        if user_id in self._leases:
            raise ProtocolError(ErrorCode.RECORDING_IN_PROGRESS, CloseCode.CONFLICT)
        if len(self._leases) >= self.max_sessions:
            raise ProtocolError(ErrorCode.SERVER_BUSY, CloseCode.TRY_AGAIN_LATER)
        lease = RecordingLease(self, user_id)
        self._leases[user_id] = lease
        return lease

    def _release(self, lease: RecordingLease) -> None:
        if self._leases.get(lease.user_id) is lease:
            del self._leases[lease.user_id]

    async def shutdown(self, grace_seconds: float) -> None:
        """Refuse new recordings, ask active ones to stop, and wait for them to save."""
        self._closing = True
        connections = [lease.connection for lease in self._leases.values() if lease.connection]
        for connection in connections:
            connection.request_shutdown()
        if connections:
            await asyncio.wait(
                [asyncio.ensure_future(c.wait_closed()) for c in connections], timeout=grace_seconds
            )
