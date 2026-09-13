"""Quota budget: live priorities may use the 3% buffer; the rest may not."""

from __future__ import annotations

import math
from datetime import UTC, datetime, time, timedelta

from predictor.client.quota import QuotaSnapshot
from predictor.constants import LIVE_QUOTA_PRIORITIES, LIVE_REQUESTS_PER_TICK


def quota_allows(priority: int, snapshot: QuotaSnapshot, buffer_percent: float) -> bool:
    if snapshot.remaining <= 0:
        return False
    if priority in LIVE_QUOTA_PRIORITIES:
        return True
    floor = math.ceil(snapshot.limit_day * (buffer_percent / 100.0))
    return snapshot.remaining > floor


def seconds_until_utc_midnight(now: datetime) -> float:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    utc_now = now.astimezone(UTC)
    nxt = datetime.combine(utc_now.date() + timedelta(days=1), time.min, tzinfo=UTC)
    return max(1.0, (nxt - utc_now).total_seconds())


def live_poll_interval_seconds(
    snapshot: QuotaSnapshot,
    target_seconds: int,
    now: datetime,
    *,
    requests_per_tick: int = LIVE_REQUESTS_PER_TICK,
) -> float:
    """Return the sleep between live ticks. Stretch past the target when quota
    cannot sustain it until UTC midnight (FR-019)."""
    target = float(target_seconds)
    if snapshot.remaining <= 0:
        return seconds_until_utc_midnight(now)
    cost = max(1, requests_per_tick)
    seconds_left = seconds_until_utc_midnight(now)
    projected = (seconds_left / target) * cost
    if projected <= snapshot.remaining:
        return target
    max_ticks = snapshot.remaining // cost
    if max_ticks <= 0:
        return seconds_until_utc_midnight(now)
    return max(target, seconds_left / max_ticks)
