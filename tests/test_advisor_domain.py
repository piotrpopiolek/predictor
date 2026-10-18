"""Pure advisor rules: period, playability, coupons, settlement."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from predictor.services.advisor.domain import (
    CandidateView,
    FinalScore,
    LineQuote,
    Period,
    PlayLimits,
    Settlement,
    Skip,
    active_combined_odd,
    assess,
    combined_odd,
    coupon_within_limits,
    match_minute,
    over_under_market,
    period_for,
    settle_coupon,
    settle_leg,
    settled_pnl,
    wait_continues,
)

NOW = datetime(2026, 10, 18, 16, 0, tzinfo=UTC)
LIMITS = PlayLimits(
    max_minute_regulation=80,
    max_minute_extra=110,
    max_quote_age=timedelta(seconds=90),
)
ODD = Decimal("1.80")


def _quote(
    odd: Decimal | None = ODD,
    tradable: bool = True,
    age: timedelta = timedelta(seconds=10),
    captured: datetime | None = None,
) -> LineQuote:
    stamp = NOW - age if captured is None else captured
    return LineQuote(odd=odd, tradable=tradable, captured_at=stamp)


def _view(**overrides: object) -> CandidateView:
    base = CandidateView(
        fixture_id=10,
        status_short="2H",
        elapsed=70,
        extra=None,
        goals_home=0,
        goals_away=1,
        extratime_home=None,
        extratime_away=None,
        quote=_quote(),
        market_known=True,
        in_open_coupon=False,
        state_rejected=False,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _final(**overrides: object) -> FinalScore:
    base = FinalScore(
        status_short="FT",
        fulltime_home=1,
        fulltime_away=1,
        extratime_home=None,
        extratime_away=None,
        final_refreshed=True,
        awaiting_manual=False,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_period_for_splits_regulation_extra_time_and_the_rest() -> None:
    assert period_for("1H") is Period.REGULATION
    assert period_for("HT") is Period.REGULATION
    assert period_for("2H") is Period.REGULATION
    assert period_for("ET") is Period.EXTRA_TIME
    assert period_for("BT") is Period.EXTRA_TIME
    assert period_for("P") is None
    assert period_for("PEN") is None
    assert period_for("FT") is None
    assert period_for("SUSP") is None


def test_extra_time_market_is_unknown_and_not_the_regulation_line() -> None:
    assert over_under_market(Period.REGULATION) == "over/under line"
    assert over_under_market(Period.EXTRA_TIME) is None


def test_match_minute_uses_the_api_clock_in_extra_time() -> None:
    assert match_minute("HT", None, None) == 45
    assert match_minute("HT", 45, 2) == 47
    assert match_minute("2H", 80, 1) == 81
    assert match_minute("2H", None, None) is None
    assert match_minute("ET", 105, None) == 105
    assert match_minute("ET", 110, None) == 110
    assert match_minute("BT", 110, 1) == 111
    assert match_minute("ET", None, None) is None
    assert match_minute("1H", 30, 1) == 31


def test_playable_regulation_candidate_uses_scoreboard_as_n() -> None:
    result = assess(_view(), LIMITS, NOW)
    assert result.playable
    assert result.period is Period.REGULATION
    assert result.n == 1
    assert result.state is not None
    assert result.state.goals_home == 0
    assert result.state.goals_away == 1
    assert result.state.ordinal == 2


def test_extra_time_n_ignores_the_regulation_score_and_an_unknown_market() -> None:
    result = assess(
        _view(
            status_short="ET",
            elapsed=100,
            goals_home=1,
            goals_away=1,
            extratime_home=1,
            extratime_away=0,
            market_known=False,
        ),
        LIMITS,
        NOW,
    )
    assert result.period is Period.EXTRA_TIME
    assert result.n == 1
    assert result.state is not None
    assert result.state.ordinal == 2
    assert (result.state.goals_home, result.state.goals_away) == (1, 1)


def test_known_extra_time_market_is_playable_at_minute_110() -> None:
    # The market name is injected by the caller once /odds/live/bets names it.
    result = assess(
        _view(status_short="ET", elapsed=110, market_known=True),
        LIMITS,
        NOW,
    )
    assert result.playable
    assert result.n == 0


def test_penalties_and_finished_matches_are_not_playable() -> None:
    assert assess(_view(status_short="P"), LIMITS, NOW).code is Skip.PENALTIES
    assert assess(_view(status_short="FT"), LIMITS, NOW).code is Skip.NOT_IN_PLAY
    assert assess(_view(status_short="SUSP"), LIMITS, NOW).code is Skip.NOT_IN_PLAY
    assert assess(_view(goals_home=None), LIMITS, NOW).code is Skip.NOT_IN_PLAY


def test_minute_cutoff_allows_the_limit_and_half_time_without_a_clock() -> None:
    assert assess(_view(elapsed=80), LIMITS, NOW).playable
    assert assess(_view(elapsed=80, extra=1), LIMITS, NOW).code is Skip.MINUTE_CUTOFF
    assert assess(_view(status_short="HT", elapsed=None), LIMITS, NOW).playable
    assert assess(_view(elapsed=None), LIMITS, NOW).code is Skip.MINUTE_CUTOFF
    assert assess(_view(status_short="ET", elapsed=110), LIMITS, NOW).playable
    extra = assess(_view(status_short="ET", elapsed=110, extra=1), LIMITS, NOW)
    assert extra.code is Skip.MINUTE_CUTOFF


def test_missing_suspended_and_stale_quotes() -> None:
    assert assess(_view(quote=None), LIMITS, NOW).code is Skip.NO_LINE
    assert assess(_view(quote=_quote(odd=None)), LIMITS, NOW).code is Skip.NO_LINE
    assert assess(_view(quote=_quote(odd=Decimal("1"))), LIMITS, NOW).code is (
        Skip.NO_LINE
    )
    assert assess(_view(market_known=False), LIMITS, NOW).code is Skip.NO_LINE
    suspended = _quote(tradable=False, age=timedelta(seconds=1000))
    assert assess(_view(quote=suspended), LIMITS, NOW).code is Skip.SUSPENDED
    assert assess(_view(quote=_quote(age=timedelta(seconds=90))), LIMITS, NOW).playable
    assert (
        assess(_view(quote=_quote(age=timedelta(seconds=91))), LIMITS, NOW).code
        is Skip.STALE_ODD
    )
    assert assess(_view(quote=_quote(captured=NOW)), LIMITS, NOW).playable
    future = _quote(captured=NOW + timedelta(seconds=5))
    assert assess(_view(quote=future), LIMITS, NOW).code is Skip.STALE_ODD
    naive = LineQuote(odd=ODD, tradable=True, captured_at=datetime(2026, 10, 18))
    assert assess(_view(quote=naive), LIMITS, NOW).code is Skip.STALE_ODD
    missing_time = LineQuote(odd=ODD, tradable=True, captured_at=None)
    assert assess(_view(quote=missing_time), LIMITS, NOW).code is Skip.STALE_ODD


def test_open_coupon_and_rejected_state_are_checked_after_the_quote() -> None:
    assert assess(_view(in_open_coupon=True), LIMITS, NOW).code is Skip.OPEN_COUPON
    assert assess(_view(state_rejected=True), LIMITS, NOW).code is (Skip.REJECTED_STATE)
    # A missing line wins over a rejection so the recorded reason is the market.
    missing = assess(_view(quote=None, state_rejected=True), LIMITS, NOW)
    assert missing.code is Skip.NO_LINE


def test_wait_does_not_cross_periods() -> None:
    assert wait_continues(Period.REGULATION, Period.REGULATION)
    assert not wait_continues(Period.REGULATION, Period.EXTRA_TIME)


def test_coupon_threshold_and_leg_count() -> None:
    low = Decimal("1.40")
    high = Decimal("1.20")
    assert coupon_within_limits((low, high), min_odd=Decimal("1.6"), max_legs=3)
    assert combined_odd((low, high)) == Decimal("1.680")
    assert not coupon_within_limits((low,), min_odd=Decimal("1.6"), max_legs=3)
    assert not coupon_within_limits(
        (low, low, low, low), min_odd=Decimal("1.6"), max_legs=3
    )
    assert not coupon_within_limits((), min_odd=Decimal("1.6"), max_legs=3)
    assert not coupon_within_limits((Decimal("1"),), min_odd=Decimal("1.6"), max_legs=3)
    assert coupon_within_limits((Decimal("1.600"),), min_odd=Decimal("1.6"), max_legs=3)
    with pytest.raises(ValueError):
        combined_odd(())
    with pytest.raises(ValueError):
        combined_odd((Decimal("0.9"),))


def test_regulation_goal_in_stoppage_wins_and_extra_time_goal_does_not() -> None:
    # N was 1 (0-1). Full time 1-1, including a stoppage-time goal, wins.
    assert settle_leg(Period.REGULATION, 1, _final()) is Settlement.WON
    # Same full-time score, goal only after 90 minutes: regulation stays 0-1.
    only_extra = _final(
        status_short="AET",
        fulltime_home=0,
        fulltime_away=1,
        extratime_home=1,
        extratime_away=0,
    )
    assert settle_leg(Period.REGULATION, 1, only_extra) is Settlement.LOST


def test_extra_time_leg_uses_extra_time_goals_including_its_stoppage() -> None:
    score = _final(
        status_short="AET",
        fulltime_home=1,
        fulltime_away=1,
        extratime_home=0,
        extratime_away=1,
    )
    assert settle_leg(Period.EXTRA_TIME, 0, score) is Settlement.WON
    no_extra_goal = replace(score, extratime_home=0, extratime_away=0)
    assert settle_leg(Period.EXTRA_TIME, 0, no_extra_goal) is Settlement.LOST
    # Penalties are not in the extra-time columns, so they cannot win the leg.
    after_penalties = replace(score, status_short="PEN", extratime_away=0)
    assert settle_leg(Period.EXTRA_TIME, 0, after_penalties) is Settlement.LOST


def test_settlement_waits_for_full_time_and_the_post_match_refresh() -> None:
    live = _final(status_short="2H", final_refreshed=False)
    assert settle_leg(Period.REGULATION, 1, live) is Settlement.PENDING
    stale = _final(final_refreshed=False)
    assert settle_leg(Period.REGULATION, 1, stale) is Settlement.PENDING
    abandoned = _final(status_short="ABD")
    assert settle_leg(Period.REGULATION, 1, abandoned) is Settlement.VOID
    assert settle_leg(Period.EXTRA_TIME, 0, abandoned) is Settlement.VOID


def test_missing_score_or_a_day_without_full_time_needs_a_person() -> None:
    stuck = _final(status_short="2H", awaiting_manual=True)
    assert settle_leg(Period.REGULATION, 1, stuck) is Settlement.MANUAL
    blank = _final(fulltime_home=None)
    assert settle_leg(Period.REGULATION, 1, blank) is Settlement.MANUAL
    no_extra = _final(status_short="AET", extratime_home=None, extratime_away=None)
    assert settle_leg(Period.EXTRA_TIME, 0, no_extra) is Settlement.MANUAL


def test_coupon_settlement_voids_and_losses() -> None:
    assert settle_coupon((Settlement.WON, Settlement.WON)) is Settlement.WON
    assert settle_coupon((Settlement.WON, Settlement.VOID)) is Settlement.WON
    assert settle_coupon((Settlement.LOST, Settlement.VOID)) is Settlement.LOST
    assert settle_coupon((Settlement.VOID, Settlement.VOID)) is Settlement.VOID
    assert settle_coupon((Settlement.WON, Settlement.PENDING)) is Settlement.PENDING
    assert settle_coupon((Settlement.WON, Settlement.MANUAL)) is Settlement.MANUAL
    assert settle_coupon((Settlement.LOST, Settlement.PENDING)) is Settlement.LOST
    assert settle_coupon((Settlement.LOST, Settlement.MANUAL)) is Settlement.LOST
    with pytest.raises(ValueError):
        settle_coupon(())


def test_active_odd_drops_void_legs_and_pnl_keeps_the_twelve_percent() -> None:
    legs = (
        (Decimal("1.50"), Settlement.WON),
        (Decimal("1.40"), Settlement.VOID),
    )
    assert active_combined_odd(legs) == Decimal("1.500")
    assert active_combined_odd(((Decimal("1.50"), Settlement.VOID),)) is None
    assert active_combined_odd(((Decimal("1.50"), Settlement.PENDING),)) is None
    stake = Decimal("10.00")
    assert settled_pnl(stake, Decimal("1.500"), Settlement.WON) == Decimal("3.20")
    assert settled_pnl(stake, Decimal("1.500"), Settlement.LOST) == Decimal("-10.00")
    assert settled_pnl(stake, Decimal("1.500"), Settlement.VOID) == Decimal("0.00")
    with pytest.raises(ValueError):
        settled_pnl(stake, Decimal("1.500"), Settlement.PENDING)


def test_same_final_score_settles_the_same_way_twice() -> None:
    score = _final()
    assert settle_leg(Period.REGULATION, 1, score) == settle_leg(
        Period.REGULATION, 1, score
    )
