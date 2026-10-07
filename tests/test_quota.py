from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from predictor.client.quota import QuotaSnapshot
from predictor.services.quota import (
    ContextCredit,
    DayLoadForecast,
    KickoffLoad,
    context_allowance,
    live_poll_interval_gauge,
    live_poll_interval_seconds,
    plan_live_budget,
    quota_allows,
    quota_gauges_from_params,
    safety_buffer_requests,
    seconds_until_utc_midnight,
)


def _plan(matches: int, remaining: int, now: datetime):
    return plan_live_budget(
        live_matches=matches, remaining=remaining, now=now, target_seconds=60
    )


def test_history_keeps_the_calls_above_the_live_half() -> None:
    now = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    snap = QuotaSnapshot(current=7100, limit_day=7500, remaining=400, source="api")
    plan = _plan(30, snap.remaining, now)
    assert plan.score_poll_seconds == 60.0
    assert plan.full_reserve <= 400
    assert plan.overloaded is False
    assert quota_allows(1, snap, plan) is True
    assert quota_allows(2, snap, plan) is True
    assert quota_allows(9, snap, plan) is True


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
    assert plan.detail_refresh_seconds > 300
    assert plan.oneshot_calls == 0
    assert plan.overloaded is False
    assert plan.score_poll_seconds == 60.0
    assert plan.full_reserve <= 200


def test_oneshots_stop_when_score_reserve_is_the_whole_budget() -> None:
    now = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    plan = _plan(30, 200, now)
    assert plan.score_need == 144
    assert plan.detail_need == 0
    assert plan.odds_need == 0
    assert plan.oneshot_calls == 0
    assert plan.overloaded is False
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
    assert plan.score_need == 288
    assert plan.score_poll_seconds == 300.0
    assert plan.poll_odds is False
    assert plan.oneshot_calls == 0
    assert plan.overloaded is False
    assert plan.detail_refresh_seconds > 300
    assert plan.full_reserve <= 1000
    snap = QuotaSnapshot(current=5500, limit_day=7500, remaining=2000, source="api")
    assert quota_allows(8, snap, plan) is True
    at_reserve = QuotaSnapshot(
        current=6500, limit_day=7500, remaining=plan.full_reserve, source="api"
    )
    assert quota_allows(8, at_reserve, plan) is False
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


def test_idle_fresh_start_keeps_the_usable_plan() -> None:
    now = datetime(2026, 9, 26, 9, 38, tzinfo=UTC)
    assert safety_buffer_requests(7500, 5) == 375
    plan = plan_live_budget(
        live_matches=0,
        remaining=7500,
        now=now,
        limit_day=7500,
        buffer_percent=5,
    )
    assert plan.safety_buffer == 375
    assert 7500 - plan.safety_buffer == 7125
    assert plan.reserved_calls <= 7125


def test_restart_uses_remaining_instead_of_recreating_the_plan() -> None:
    now = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
    plan = plan_live_budget(
        live_matches=12,
        remaining=4000,
        now=now,
        limit_day=7500,
        buffer_percent=5,
    )
    assert plan.safety_buffer == 375
    assert plan.reserved_calls <= 4000 - 375


def test_large_future_slate_does_not_clamp_detail_to_fifteen_minutes() -> None:
    now = datetime(2026, 9, 26, 9, 38, tzinfo=UTC)
    later = DayLoadForecast(
        kickoffs=(KickoffLoad(datetime(2026, 9, 26, 13, 0, tzinfo=UTC), 1000),)
    )
    crowded = plan_live_budget(
        live_matches=40,
        forecast=later,
        remaining=6315,
        now=now,
        limit_day=7500,
        buffer_percent=5,
    )
    assert crowded.overloaded is False
    assert crowded.detail_refresh_seconds > 300
    assert crowded.score_poll_seconds == 60.0
    assert crowded.reserved_calls <= 6315 - 375
    snap = QuotaSnapshot(current=1185, limit_day=7500, remaining=6315, source="api")
    assert quota_allows(9, snap, crowded) is True
    calm = plan_live_budget(
        live_matches=40,
        remaining=6315,
        now=now,
        limit_day=7500,
        buffer_percent=5,
    )
    assert calm.detail_refresh_seconds == 300
    assert calm.overloaded is False


def test_same_budget_changes_with_kickoff_distribution() -> None:
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    short = plan_live_budget(
        live_matches=0,
        forecast=DayLoadForecast(
            kickoffs=(KickoffLoad(datetime(2026, 9, 26, 23, 0, tzinfo=UTC), 80),)
        ),
        remaining=7500,
        now=now,
        limit_day=7500,
        buffer_percent=5,
    )
    full = plan_live_budget(
        live_matches=0,
        forecast=DayLoadForecast(
            kickoffs=(KickoffLoad(datetime(2026, 9, 26, 18, 0, tzinfo=UTC), 80),)
        ),
        remaining=7500,
        now=now,
        limit_day=7500,
        buffer_percent=5,
    )
    assert short.detail_need < full.detail_need
    assert short.score_need < full.score_need


def test_low_day_keeps_five_minute_detail_and_oneshots() -> None:
    now = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
    plan = plan_live_budget(
        live_matches=5,
        remaining=7000,
        now=now,
        limit_day=7500,
        buffer_percent=5,
    )
    assert plan.detail_refresh_seconds == 300
    assert plan.oneshot_calls == 12
    assert plan.overloaded is False
    assert plan.score_poll_seconds == 60.0
    assert plan.reserved_calls <= 7000 - plan.safety_buffer


def test_buffer_blocks_every_priority_including_scores() -> None:
    now = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    snap = QuotaSnapshot(current=7200, limit_day=7500, remaining=300, source="api")
    plan = plan_live_budget(
        live_matches=8,
        remaining=snap.remaining,
        now=now,
        limit_day=7500,
        buffer_percent=5,
    )
    assert plan.safety_buffer == 375
    for priority in range(1, 10):
        assert quota_allows(priority, snap, plan) is False


def test_context_credit_smooths_fractional_ticks_and_respects_room() -> None:
    credit = ContextCredit()
    assert context_allowance(credit, 2.5, room=10) == 2
    assert context_allowance(credit, 2.5, room=10) == 3
    blocked = ContextCredit()
    assert context_allowance(blocked, 5, room=0) == 0
    assert context_allowance(blocked, 5, room=10) == 10


def test_heavy_day_simulation_stays_above_the_safety_buffer() -> None:
    start = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
    midnight = datetime(2026, 9, 27, 0, 0, tzinfo=UTC)
    schedule = (
        (datetime(2026, 9, 26, 13, 0, tzinfo=UTC), 400),
        (datetime(2026, 9, 26, 15, 0, tzinfo=UTC), 400),
        (datetime(2026, 9, 26, 18, 0, tzinfo=UTC), 200),
    )
    remaining = 7500
    buffer = safety_buffer_requests(7500, 5)
    credit = ContextCredit()
    now = start
    steps = 0
    while now < midnight and steps < 2000:
        steps += 1
        live = 0
        future: list[KickoffLoad] = []
        for kickoff, count in schedule:
            if kickoff <= now < kickoff + timedelta(hours=2):
                live += count
            elif now < kickoff:
                future.append(KickoffLoad(kickoff, count))
        plan = plan_live_budget(
            live_matches=live,
            forecast=DayLoadForecast(tuple(future)),
            remaining=remaining,
            now=now,
            target_seconds=60,
            limit_day=7500,
            buffer_percent=5,
        )
        assert plan.reserved_calls <= max(0, remaining - buffer)
        if remaining <= buffer:
            break
        score = 1 if remaining - 1 >= buffer else 0
        room = remaining - score - buffer
        context = context_allowance(
            credit, plan.context_calls_per_tick, room=max(0, room)
        )
        odds = 1 if plan.poll_odds and not plan.overloaded and room - context > 0 else 0
        spent = score + context + odds
        assert remaining - spent >= buffer
        remaining -= spent
        now += timedelta(seconds=max(1.0, plan.score_poll_seconds))
    assert remaining >= buffer
    assert steps < 2000


def test_future_slate_does_not_force_the_old_fifteen_minute_ceiling() -> None:
    now = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
    later = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    plan = plan_live_budget(
        live_matches=40,
        forecast=DayLoadForecast(kickoffs=(KickoffLoad(later, 800),)),
        remaining=7500,
        now=now,
        target_seconds=60,
        limit_day=7500,
        buffer_percent=5,
    )
    assert plan.detail_refresh_seconds > 300
    assert plan.overloaded is False
    assert plan.score_poll_seconds == 60.0
    assert plan.safety_buffer == 375
    usable = 7500 - 375
    assert usable - plan.reserved_calls >= int(usable * 0.5)


def test_fresh_start_allocates_the_plan_above_the_buffer() -> None:
    now = datetime(2026, 9, 26, 9, 38, tzinfo=UTC)
    assert safety_buffer_requests(7500, 5) == 375
    plan = plan_live_budget(
        live_matches=40,
        forecast=DayLoadForecast(
            kickoffs=(KickoffLoad(datetime(2026, 9, 26, 18, 0, tzinfo=UTC), 800),)
        ),
        remaining=7500,
        now=now,
        target_seconds=60,
        limit_day=7500,
        buffer_percent=5,
    )
    assert plan.safety_buffer == 375
    assert plan.reserved_calls <= 7125
    restart = plan_live_budget(
        live_matches=40,
        forecast=DayLoadForecast(
            kickoffs=(KickoffLoad(datetime(2026, 9, 26, 18, 0, tzinfo=UTC), 800),)
        ),
        remaining=4000,
        now=now,
        target_seconds=60,
        limit_day=7500,
        buffer_percent=5,
    )
    assert restart.safety_buffer == 375
    assert restart.reserved_calls <= 4000 - 375
    assert restart.reserved_calls < plan.reserved_calls


def test_same_budget_changes_cap_when_later_kickoffs_change() -> None:
    now = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
    common = {
        "live_matches": 40,
        "remaining": 7500,
        "now": now,
        "target_seconds": 60,
        "limit_day": 7500,
        "buffer_percent": 5,
    }
    light = plan_live_budget(
        **common,
        forecast=DayLoadForecast(
            kickoffs=(KickoffLoad(datetime(2026, 9, 26, 18, 0, tzinfo=UTC), 30),)
        ),
    )
    heavy = plan_live_budget(
        **common,
        forecast=DayLoadForecast(
            kickoffs=(KickoffLoad(datetime(2026, 9, 26, 18, 0, tzinfo=UTC), 800),)
        ),
    )
    assert light.detail_refresh_seconds == 300
    assert light.oneshot_calls == 4
    assert light.overloaded is False
    assert heavy.relaxed_refresh_seconds > light.relaxed_refresh_seconds
    assert heavy.context_calls_per_tick < light.context_calls_per_tick


def test_light_day_keeps_five_minute_details_and_oneshots() -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    plan = plan_live_budget(
        live_matches=8,
        remaining=7000,
        now=now,
        target_seconds=60,
        limit_day=7500,
        buffer_percent=5,
    )
    assert plan.detail_refresh_seconds == 300
    assert plan.oneshot_calls == 12
    assert plan.score_poll_seconds == 60.0
    assert plan.overloaded is False
    assert plan.poll_odds is True


def test_buffer_blocks_work_that_would_spend_it() -> None:
    now = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    plan = plan_live_budget(
        live_matches=10,
        remaining=375,
        now=now,
        target_seconds=60,
        limit_day=7500,
        buffer_percent=5,
    )
    snap = QuotaSnapshot(current=7125, limit_day=7500, remaining=375, source="api")
    assert plan.safety_buffer == 375
    for priority in range(1, 10):
        assert quota_allows(priority, snap, plan) is False


def _spend_day(
    start: datetime,
    kickoffs: list[tuple[datetime, int]],
    remaining: int = 7500,
) -> tuple[int, bool]:
    credit = ContextCredit()
    now = start
    midnight = datetime(start.year, start.month, start.day, tzinfo=UTC) + timedelta(
        days=1
    )
    overloaded = False
    for _ in range(6000):
        if now >= midnight:
            break
        live = 0
        future: list[KickoffLoad] = []
        for kickoff, count in kickoffs:
            if kickoff <= now < kickoff + timedelta(hours=2):
                live += count
            elif kickoff > now:
                future.append(KickoffLoad(kickoff, count))
        plan = plan_live_budget(
            live_matches=live,
            forecast=DayLoadForecast(tuple(future)),
            remaining=remaining,
            now=now,
            target_seconds=60,
            limit_day=7500,
            buffer_percent=5,
        )
        overloaded = overloaded or plan.overloaded
        snap = QuotaSnapshot(
            current=7500 - remaining,
            limit_day=7500,
            remaining=remaining,
            source="api",
        )
        room = remaining - plan.safety_buffer
        if room <= 0:
            break
        score = 1 if quota_allows(2, snap, plan) else 0
        odds = 1 if quota_allows(4, snap, plan) else 0
        if score + odds > room:
            odds = 0
        context = 0
        if quota_allows(3, snap, plan):
            context = context_allowance(
                credit,
                plan.context_calls_per_tick,
                room=room - score - odds,
            )
        remaining -= score + odds + context
        assert remaining >= plan.safety_buffer
        now += timedelta(seconds=max(1.0, plan.score_poll_seconds))
    return remaining, overloaded


def test_heavy_day_from_a_fresh_plan_stays_above_the_buffer() -> None:
    day = datetime(2026, 9, 26, tzinfo=UTC)
    kickoffs = [
        (day.replace(hour=11), 200),
        (day.replace(hour=14), 300),
        (day.replace(hour=16), 300),
        (day.replace(hour=19), 200),
    ]
    from_midnight, _overloaded = _spend_day(day, kickoffs)
    assert from_midnight >= 375
    assert from_midnight < 7500
    from_midday, _midday_overloaded = _spend_day(
        day.replace(hour=9, minute=38), kickoffs
    )
    assert from_midday >= 375
