from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from predictor.client.quota import QuotaSnapshot
from predictor.services.quota import (
    live_poll_interval_gauge,
    live_poll_interval_seconds,
    plan_live_budget,
    quota_allows,
    quota_gauges_from_params,
    seconds_until_utc_midnight,
)


def _plan(matches: int, remaining: int, now: datetime):
    return plan_live_budget(
        live_matches=matches, remaining=remaining, now=now, target_seconds=60
    )


def test_history_stops_when_live_reserve_is_touched() -> None:
    now = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    snap = QuotaSnapshot(current=7100, limit_day=7500, remaining=400, source="api")
    plan = _plan(30, snap.remaining, now)
    assert plan.full_reserve > 400
    assert quota_allows(1, snap, plan) is True
    assert quota_allows(2, snap, plan) is True
    assert quota_allows(5, snap, plan) is False
    assert quota_allows(9, snap, plan) is False
    assert quota_allows(4, snap, plan) is False


def test_surplus_allows_history_and_five_minute_details() -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    snap = QuotaSnapshot(current=500, limit_day=7500, remaining=7000, source="api")
    plan = _plan(30, snap.remaining, now)
    assert quota_allows(7, snap, plan) is True
    assert quota_allows(4, snap, plan) is True
    assert plan.detail_refresh_seconds == 300
    assert plan.oneshot_calls == 12
    assert plan.score_poll_seconds == 60.0


def test_tight_reserve_slows_details_and_drops_oneshots() -> None:
    now = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    plan = _plan(30, 400, now)
    assert plan.detail_refresh_seconds == 900
    assert plan.oneshot_calls == 0
    assert plan.score_poll_seconds == 60.0


def test_matches_still_to_play_today_are_reserved_before_kickoff() -> None:
    now = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)
    plan = plan_live_budget(
        live_matches=0,
        matches_left_today=40,
        remaining=2000,
        now=now,
        target_seconds=60,
    )
    assert plan.detail_need == 40 * 2 * 12
    assert plan.odds_need > 0
    assert plan.score_poll_seconds == 300.0
    assert plan.poll_odds is False
    snap = QuotaSnapshot(current=5500, limit_day=7500, remaining=2000, source="api")
    assert quota_allows(8, snap, plan) is False
    assert quota_allows(2, snap, plan) is True


def test_empty_board_polls_scores_every_five_minutes() -> None:
    now = datetime(2026, 9, 23, 3, 0, tzinfo=UTC)
    plan = _plan(0, 7000, now)
    assert plan.odds_need == 0
    assert plan.detail_need == 0
    assert plan.score_poll_seconds == 300.0
    snap = QuotaSnapshot(current=500, limit_day=7500, remaining=7000, source="api")
    assert quota_allows(4, snap, plan) is False
    assert quota_allows(8, snap, plan) is True


def test_remaining_zero_blocks_all() -> None:
    now = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    snap = QuotaSnapshot(current=7500, limit_day=7500, remaining=0, source="api")
    plan = _plan(10, 0, now)
    for priority in range(1, 10):
        assert quota_allows(priority, snap, plan) is False


def test_last_live_requests_stay_reserved_for_score_poll() -> None:
    now = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    snap = QuotaSnapshot(current=7498, limit_day=7500, remaining=2, source="api")
    plan = _plan(10, 2, now)
    assert quota_allows(1, snap, plan) is True
    assert quota_allows(2, snap, plan) is True
    assert quota_allows(3, snap, plan) is False
    assert quota_allows(4, snap, plan) is False


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
