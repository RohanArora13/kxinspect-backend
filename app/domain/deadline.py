"""Pure deadline policy. UTC only; expiry is inclusive of the boundary instant."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.clock import to_utc


def deadline_for(raised_at: datetime, grace_period_days: int) -> datetime:
    """``deadlineAt = raisedAt + gracePeriodDays``, evaluated in UTC.

    Whole-day arithmetic in UTC keeps the boundary stable across daylight-saving shifts
    in the display time zone.
    """
    if grace_period_days < 0:
        raise ValueError("gracePeriodDays cannot be negative")
    return to_utc(raised_at) + timedelta(days=grace_period_days)


def is_expired(now: datetime, deadline_at: datetime) -> bool:
    """Expiry is ``now >= deadlineAt``: the boundary instant is already elapsed."""
    return to_utc(now) >= to_utc(deadline_at)


def seconds_until(now: datetime, deadline_at: datetime) -> float:
    """Non-negative seconds remaining; zero once the boundary has been reached."""
    remaining = (to_utc(deadline_at) - to_utc(now)).total_seconds()
    return max(remaining, 0.0)
