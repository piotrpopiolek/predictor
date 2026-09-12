"""Quota budget: live priorities may use the 3% buffer; the rest may not."""

from __future__ import annotations

import math
from datetime import UTC, datetime, time, timedelta

from predictor.client.quota import QuotaSnapshot
from predictor.constants import LIVE_QUOTA_PRIORITIES


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
