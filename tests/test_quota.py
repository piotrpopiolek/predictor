from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone

import pytest

from predictor.client.quota import QuotaSnapshot
from predictor.services.quota import (
    live_poll_interval_gauge,
    live_poll_interval_seconds,
    quota_allows,
    quota_gauges_from_params,
    seconds_until_utc_midnight,
)


def test_live_priorities_may_use_buffer() -> None:
    snap = QuotaSnapshot(current=7490, limit_day=7500, remaining=10, source="api")
    assert quota_allows(1, snap, 5.0) is True
    assert quota_allows(2, snap, 5.0) is True
    assert quota_allows(3, snap, 5.0) is True
    assert quota_allows(5, snap, 5.0) is False
    assert quota_allows(8, snap, 5.0) is False


def test_non_live_allowed_above_buffer_floor() -> None:
    floor = math.ceil(7500 * 0.05)
    snap = QuotaSnapshot(
        current=7500 - floor - 1, limit_day=7500, remaining=floor + 1, source="api"
    )
    assert quota_allows(7, snap, 5.0) is True
    snap_eq = QuotaSnapshot(
        current=7500 - floor, limit_day=7500, remaining=floor, source="api"
    )
    assert quota_allows(7, snap_eq, 5.0) is False


def test_remaining_zero_blocks_all() -> None:
    snap = QuotaSnapshot(current=100, limit_day=100, remaining=0, source="api")
    for priority in range(1, 9):
        assert quota_allows(priority, snap, 5.0) is False


def test_seconds_until_utc_midnight() -> None:
    now = datetime(2026, 9, 12, 23, 0, 0, tzinfo=UTC)
    assert seconds_until_utc_midnight(now) == 3600.0


def test_seconds_until_utc_midnight_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        seconds_until_utc_midnight(datetime(2026, 9, 12, 23, 0, 0))


def test_quota_reset_matches_vendor_dashboard_countdown() -> None:
    """dashboard.api-football.com: reset at 00h00 UTC, not local midnight.

    Screenshot 2026-09-13 22:01 Europe/Warsaw (UTC+2) showed 03H59 remaining.
    """
    warsaw_summer = timezone(timedelta(hours=2))
    now = datetime(2026, 9, 13, 22, 1, tzinfo=warsaw_summer)
    assert seconds_until_utc_midnight(now) == 3 * 3600 + 59 * 60
    local_midnight = datetime(2026, 9, 14, 0, 0, tzinfo=warsaw_summer)
    assert seconds_until_utc_midnight(now) != (local_midnight - now).total_seconds()


def test_quota_reset_stays_utc_midnight_on_winter_offset() -> None:
    # 22:01 UTC+1 is 21:01 UTC → 02H59 until 00:00 UTC (01:00 local).
    warsaw_winter = timezone(timedelta(hours=1))
    now = datetime(2026, 1, 13, 22, 1, tzinfo=warsaw_winter)
    assert seconds_until_utc_midnight(now) == 2 * 3600 + 59 * 60


def test_live_interval_stays_at_target_when_quota_allows() -> None:
    now = datetime(2026, 9, 13, 23, 0, tzinfo=UTC)
    snap = QuotaSnapshot(current=100, limit_day=7500, remaining=5000, source="api")
    assert live_poll_interval_seconds(snap, 60, now) == 60.0


def test_live_interval_stretches_when_quota_cannot_hold_target() -> None:
    now = datetime(2026, 9, 13, 23, 0, tzinfo=UTC)
    snap = QuotaSnapshot(current=7496, limit_day=7500, remaining=4, source="api")
    interval = live_poll_interval_seconds(snap, 60, now)
    assert interval == 1800.0


def test_live_poll_interval_gauge_unknown_remaining_uses_target() -> None:
    now = datetime(2026, 9, 13, 23, 0, tzinfo=UTC)
    assert live_poll_interval_gauge(-1, 0, 7500, 60, now) == 60.0


def test_live_poll_interval_gauge_stretches_like_scheduler() -> None:
    now = datetime(2026, 9, 13, 23, 0, tzinfo=UTC)
    assert live_poll_interval_gauge(4, 7496, 7500, 60, now) == 1800.0


def test_quota_gauges_from_params() -> None:
    assert quota_gauges_from_params(None) == (-1, 0)
    assert quota_gauges_from_params({}) == (-1, 0)
    remaining, used = quota_gauges_from_params({"remaining": 1200, "current": 6300})
    assert remaining == 1200
    assert used == 6300
