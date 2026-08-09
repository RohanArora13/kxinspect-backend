"""Injected time and scheduling.

No module outside this one calls ``datetime.now`` or ``time.monotonic``; tests build the
app with :class:`ManualClock` so deadline reconciliation is exact instead of flaky.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """Wall clock restricted to timezone-aware UTC instants."""

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    """Production clock."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        return asyncio.get_event_loop().time()


class ManualClock:
    """Deterministic clock for tests and for the anchored demo mode."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware instant")
        self._now = start.astimezone(UTC)
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("ManualClock cannot move backwards")
        self._now += delta
        self._monotonic += delta.total_seconds()

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware instant")
        self._now = moment.astimezone(UTC)


class Scheduler(Protocol):
    """Owns delayed work so tests never sleep on the wall clock."""

    def schedule(self, delay_seconds: float, callback: Callable[[], Awaitable[None]]) -> None: ...

    async def shutdown(self) -> None: ...


class AsyncioScheduler:
    """Production scheduler backed by asyncio tasks."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, delay_seconds: float, callback: Callable[[], Awaitable[None]]) -> None:
        async def runner() -> None:
            await asyncio.sleep(max(delay_seconds, 0.0))
            await callback()

        task = asyncio.create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def shutdown(self) -> None:
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        for task in pending:
            try:  # noqa: SIM105 - cancellation is the expected outcome here
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()


class ManualScheduler:
    """Records scheduled callbacks; tests fire them explicitly."""

    def __init__(self) -> None:
        self.pending: list[tuple[float, Callable[[], Awaitable[None]]]] = []

    def schedule(self, delay_seconds: float, callback: Callable[[], Awaitable[None]]) -> None:
        self.pending.append((delay_seconds, callback))

    async def fire_all(self) -> None:
        due = list(self.pending)
        self.pending.clear()
        for _, callback in due:
            await callback()

    async def shutdown(self) -> None:
        self.pending.clear()


def to_utc(moment: datetime) -> datetime:
    """Normalise an aware instant to UTC; naive instants are a contract violation."""
    if moment.tzinfo is None:
        raise ValueError("naive datetimes are rejected by the contract")
    return moment.astimezone(UTC)


def format_instant(moment: datetime) -> str:
    """Render an instant as the contract's ISO-8601 UTC form with a ``Z`` suffix."""
    normalised = to_utc(moment).replace(microsecond=moment.microsecond)
    if normalised.microsecond:
        return normalised.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return normalised.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_instant(value: str) -> datetime:
    """Parse the contract's ISO-8601 UTC form. Only ``Z`` offsets are accepted."""
    if not value.endswith("Z"):
        raise ValueError(f"instant must end with 'Z': {value!r}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"instant must be timezone-aware: {value!r}")
    return parsed.astimezone(UTC)
