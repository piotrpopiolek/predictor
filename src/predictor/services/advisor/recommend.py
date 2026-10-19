"""When a recommendation may be created, promoted, or withdrawn.

The sandbox does not read these gates. An empty stake or hour window leaves
recommendation inactive. An empty league list means every league except the
ones marked untracked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from predictor.constants import SANDBOX_BANKROLL

PROMOTION_SAMPLE = 200
MAX_DRAWDOWN = Decimal("0.25")
_RATE = Decimal("0.0001")
_ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class RecommendationSetup:
    stake: Decimal | None
    loss_limit: Decimal | None
    hours: tuple[int, int] | None
    league_ids: frozenset[int]
    coupon_limit: int
    ttl_seconds: int
    withdraw_yield: Decimal


def parse_hours(raw: str) -> tuple[int, int] | None:
    """``8-22`` is 08:00 inclusive through 22:00 exclusive. Empty is unset."""
    text = raw.strip()
    if not text:
        return None
    start_text, end_text = text.split("-", 1)
    start = int(start_text)
    end = int(end_text)
    if start < 0 or start > 23 or end < 0 or end > 24 or start == end:
        raise ValueError("hours must be start-end, with start != end")
    return start, end


def setup_for(
    *,
    stake: Decimal | None,
    loss_limit: Decimal | None,
    hours: str,
    leagues: str,
    coupon_limit: int,
    ttl_seconds: int,
    withdraw_yield: Decimal,
) -> RecommendationSetup:
    return RecommendationSetup(
        stake=stake,
        loss_limit=loss_limit,
        hours=parse_hours(hours),
        league_ids=parse_leagues(leagues),
        coupon_limit=coupon_limit,
        ttl_seconds=ttl_seconds,
        withdraw_yield=withdraw_yield,
    )


def parse_leagues(raw: str) -> frozenset[int]:
    text = raw.strip()
    if not text:
        return frozenset()
    return frozenset(int(part.strip()) for part in text.split(",") if part.strip())


def hour_allowed(hour: int, window: tuple[int, int]) -> bool:
    start, end = window
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def league_allowed(
    league_id: int,
    setup: RecommendationSetup,
    *,
    untracked: frozenset[int],
) -> bool:
    """Explicit list wins. Empty list accepts every league that is still tracked."""
    if setup.league_ids:
        return league_id in setup.league_ids
    return league_id not in untracked


def inactive_reason(
    setup: RecommendationSetup,
    now: datetime,
    *,
    coupons_today: int,
    loss_today: Decimal,
) -> str | None:
    """None when a new recommendation may be created."""
    if setup.stake is None:
        return "stake"
    if setup.loss_limit is None:
        return "loss_limit"
    if setup.hours is None:
        return "hours"
    if not hour_allowed(now.hour, setup.hours):
        return "hours"
    if coupons_today >= setup.coupon_limit:
        return "coupon_limit"
    if loss_today >= setup.loss_limit:
        return "loss_limit_hit"
    return None


def net_yield(pnl: Decimal, stakes: Decimal) -> Decimal | None:
    if stakes == 0:
        return None
    return (pnl / stakes).quantize(_RATE, rounding=ROUND_HALF_UP)


def max_drawdown(
    pnls: Sequence[Decimal], bankroll: Decimal = SANDBOX_BANKROLL
) -> Decimal:
    """Largest peak-to-trough drop divided by the starting bankroll."""
    equity = bankroll
    peak = bankroll
    worst = _ZERO
    for pnl in pnls:
        equity += pnl
        if equity > peak:
            peak = equity
        drop = peak - equity
        if drop > worst:
            worst = drop
    if bankroll == 0:
        return _ZERO
    return (worst / bankroll).quantize(_RATE, rounding=ROUND_HALF_UP)


def promotion_block(
    *,
    settled: int,
    yield_net: Decimal | None,
    baseline_yield: Decimal | None,
    drawdown: Decimal,
) -> str | None:
    """None when the strategy may become the one promoted strategy."""
    if settled < PROMOTION_SAMPLE:
        return "sample"
    if yield_net is None or yield_net <= 0:
        return "yield"
    if baseline_yield is None or yield_net <= baseline_yield:
        return "baseline"
    if drawdown > MAX_DRAWDOWN:
        return "drawdown"
    return None


def withdraw_due(yield_net: Decimal | None, floor: Decimal) -> bool:
    """A missing yield does not withdraw. A yield under the floor does."""
    if yield_net is None:
        return False
    return yield_net < floor
