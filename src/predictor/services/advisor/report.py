"""Sandbox yield against the baseline, on the same single-leg matches.

Yield is net profit after the 0.88 keep, divided by stakes of won and lost
coupons. A void returns the stake and stays out of that ratio. A combined
coupon stays in the portfolio and out of the match-by-match comparison.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.constants import SANDBOX_BANKROLL
from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorLeg,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.services.advisor.domain import Settlement, settled_pnl
from predictor.services.advisor.sandbox import portfolio_balance

_PLAYED = frozenset({"won", "lost"})
_RATE = Decimal("0.0001")
_ZERO = Decimal("0.00")
_MONEY = Decimal("0.01")
_HIT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class LegView:
    fixture_id: int
    status: str
    odd: Decimal
    period: str


@dataclass(frozen=True, slots=True)
class CouponView:
    coupon_id: int
    strategy_id: int
    status: str
    stake: Decimal
    combined_odd: Decimal | None
    payout_keep: Decimal
    legs: tuple[LegView, ...]
    placed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StrategyView:
    strategy_id: int
    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class BookLine:
    strategy_id: int
    name: str
    kind: str
    played: int
    hits: int
    hit_rate: Decimal | None
    avg_odd: Decimal | None
    stakes: Decimal
    net_pnl: Decimal
    yield_net: Decimal | None
    balance: Decimal


@dataclass(frozen=True, slots=True)
class PairYield:
    baseline_name: str
    strategy_name: str
    matches: int
    baseline_hits: int
    strategy_hits: int
    baseline_yield: Decimal | None
    strategy_yield: Decimal | None


@dataclass(frozen=True, slots=True)
class StrategyChart:
    """Chronological points shaped like /live/bets chart rows."""

    strategy_id: int
    name: str
    kind: str
    points: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class YieldReport:
    period: str
    books: tuple[BookLine, ...]
    pairs: tuple[PairYield, ...]
    charts: tuple[StrategyChart, ...] = ()


def build_report(
    strategies: Sequence[StrategyView],
    coupons: Sequence[CouponView],
    *,
    period: str,
) -> YieldReport:
    """Portfolio yield, plus baseline against each other strategy on shared matches."""
    chosen = tuple(item for item in coupons if _in_period(item, period))
    by_strategy: dict[int, list[CouponView]] = defaultdict(list)
    for coupon in chosen:
        by_strategy[coupon.strategy_id].append(coupon)
    visible = [
        item
        for item in strategies
        if item.kind in ("baseline", "llm") or item.strategy_id in by_strategy
    ]
    books = tuple(
        _book(item, by_strategy.get(item.strategy_id, [])) for item in visible
    )
    baselines = [item for item in visible if item.kind == "baseline"]
    others = [item for item in visible if item.kind != "baseline"]
    pairs = tuple(
        _pair(base, other, by_strategy) for base in baselines for other in others
    )
    charts = tuple(
        StrategyChart(
            strategy_id=item.strategy_id,
            name=item.name,
            kind=item.kind,
            points=_chart_points(by_strategy.get(item.strategy_id, [])),
        )
        for item in visible
    )
    return YieldReport(period=period, books=books, pairs=pairs, charts=charts)


def report_as_dict(report: YieldReport) -> dict[str, object]:
    return {
        "period": report.period,
        "books": [
            {
                "strategy_id": line.strategy_id,
                "name": line.name,
                "kind": line.kind,
                "played": line.played,
                "hits": line.hits,
                "hit_rate": _text(line.hit_rate),
                "avg_odd": _text(line.avg_odd),
                "stakes": _text(line.stakes),
                "net_pnl": _text(line.net_pnl),
                "yield_net": _text(line.yield_net),
                "balance": _text(line.balance),
            }
            for line in report.books
        ],
        "pairs": [
            {
                "baseline_name": pair.baseline_name,
                "strategy_name": pair.strategy_name,
                "matches": pair.matches,
                "baseline_hits": pair.baseline_hits,
                "strategy_hits": pair.strategy_hits,
                "baseline_yield": _text(pair.baseline_yield),
                "strategy_yield": _text(pair.strategy_yield),
            }
            for pair in report.pairs
        ],
        "charts": [
            {
                "strategy_id": chart.strategy_id,
                "name": chart.name,
                "kind": chart.kind,
                "points": list(chart.points),
            }
            for chart in report.charts
        ],
    }


async def load_yield_report(session: AsyncSession, *, period: str) -> YieldReport:
    """Read sandbox coupons. Does not settle them."""
    strategies = (
        await session.scalars(select(AdvisorStrategy).order_by(AdvisorStrategy.id))
    ).all()
    rows = (
        await session.execute(
            select(AdvisorCoupon, AdvisorStrategy, AdvisorLeg)
            .join(
                AdvisorStrategyVersion,
                AdvisorStrategyVersion.id == AdvisorCoupon.strategy_version_id,
            )
            .join(
                AdvisorStrategy,
                AdvisorStrategy.id == AdvisorStrategyVersion.strategy_id,
            )
            .outerjoin(AdvisorLeg, AdvisorLeg.coupon_id == AdvisorCoupon.id)
            .where(AdvisorCoupon.mode == "sandbox")
            .order_by(AdvisorCoupon.id, AdvisorLeg.id)
        )
    ).all()
    grouped: dict[int, list[LegView]] = defaultdict(list)
    owners: dict[int, tuple[AdvisorCoupon, AdvisorStrategy]] = {}
    for coupon, strategy, leg in rows:
        owners[coupon.id] = (coupon, strategy)
        if leg is None:
            continue
        grouped[coupon.id].append(
            LegView(
                fixture_id=leg.fixture_id,
                status=leg.status,
                odd=leg.odd,
                period=leg.period,
            )
        )
    coupons = tuple(
        CouponView(
            coupon_id=coupon.id,
            strategy_id=strategy.id,
            status=coupon.status,
            stake=coupon.stake,
            combined_odd=coupon.combined_odd,
            payout_keep=coupon.payout_keep,
            legs=tuple(grouped.get(coupon.id, ())),
            placed_at=coupon.placed_at or coupon.created_at,
        )
        for coupon, strategy in owners.values()
    )
    views = tuple(
        StrategyView(strategy_id=row.id, name=row.name, kind=row.kind)
        for row in strategies
    )
    return build_report(views, coupons, period=period)


def _book(strategy: StrategyView, coupons: Sequence[CouponView]) -> BookLine:
    priced = [
        coupon
        for coupon in coupons
        if coupon.status in _PLAYED and coupon.combined_odd is not None
    ]
    hits = sum(1 for coupon in priced if coupon.status == "won")
    stakes = sum((coupon.stake for coupon in priced), _ZERO)
    pnl = sum(
        (
            settled_pnl(
                coupon.stake,
                coupon.combined_odd if coupon.combined_odd is not None else _ZERO,
                Settlement(coupon.status),
                coupon.payout_keep,
            )
            for coupon in priced
        ),
        _ZERO,
    )
    safe = [
        coupon
        for coupon in coupons
        if coupon.status != "won" or coupon.combined_odd is not None
    ]
    balance = portfolio_balance(
        tuple(
            (coupon.stake, coupon.combined_odd, coupon.payout_keep, coupon.status)
            for coupon in safe
        )
    )
    return BookLine(
        strategy_id=strategy.strategy_id,
        name=strategy.name,
        kind=strategy.kind,
        played=len(priced),
        hits=hits,
        hit_rate=_ratio(Decimal(hits), Decimal(len(priced))),
        avg_odd=_avg_odd(priced),
        stakes=stakes,
        net_pnl=pnl,
        yield_net=_ratio(pnl, stakes),
        balance=balance,
    )


def _avg_odd(coupons: Sequence[CouponView]) -> Decimal | None:
    odds = [
        coupon.combined_odd for coupon in coupons if coupon.combined_odd is not None
    ]
    if not odds:
        return None
    total = sum(odds, _ZERO)
    return (total / Decimal(len(odds))).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP
    )


def _chart_points(coupons: Sequence[CouponView]) -> tuple[dict[str, Any], ...]:
    """Running saldo and hit rate, same shape as /live/bets chart input."""
    ordered = sorted(
        coupons,
        key=lambda item: (
            item.placed_at.isoformat() if item.placed_at is not None else "",
            item.coupon_id,
        ),
    )
    saldo = SANDBOX_BANKROLL
    hits = 0
    decided = 0
    points: list[dict[str, Any]] = []
    for coupon in ordered:
        placed = coupon.placed_at.isoformat() if coupon.placed_at is not None else ""
        if coupon.status == "open":
            saldo -= coupon.stake
            hit = (
                (Decimal(hits) / Decimal(decided) * Decimal(100)).quantize(
                    _HIT, rounding=ROUND_HALF_UP
                )
                if decided
                else _ZERO
            )
            points.append(
                {
                    "id": coupon.coupon_id,
                    "placed_at": placed,
                    "status": coupon.status,
                    "stake": _text(coupon.stake),
                    "pnl": "0.00",
                    "saldo": _text(saldo.quantize(_MONEY, rounding=ROUND_HALF_UP)),
                    "hit_overall": _text(hit),
                }
            )
            continue
        if coupon.status == "won" and coupon.combined_odd is not None:
            pnl = settled_pnl(
                coupon.stake,
                coupon.combined_odd,
                Settlement.WON,
                coupon.payout_keep,
            )
            saldo += pnl
            decided += 1
            hits += 1
        elif coupon.status == "lost":
            pnl = -coupon.stake
            saldo -= coupon.stake
            decided += 1
        elif coupon.status == "void":
            pnl = _ZERO
        else:
            continue
        hit = (
            (Decimal(hits) / Decimal(decided) * Decimal(100)).quantize(
                _HIT, rounding=ROUND_HALF_UP
            )
            if decided
            else _ZERO
        )
        points.append(
            {
                "id": coupon.coupon_id,
                "placed_at": placed,
                "status": coupon.status,
                "stake": _text(coupon.stake),
                "pnl": _text(pnl.quantize(_MONEY, rounding=ROUND_HALF_UP)),
                "saldo": _text(saldo.quantize(_MONEY, rounding=ROUND_HALF_UP)),
                "hit_overall": _text(hit),
            }
        )
    return tuple(points)


def _pair(
    baseline: StrategyView,
    other: StrategyView,
    by_strategy: dict[int, list[CouponView]],
) -> PairYield:
    base = _singles(by_strategy.get(baseline.strategy_id, []))
    side = _singles(by_strategy.get(other.strategy_id, []))
    shared = sorted(set(base) & set(side))
    base_coupons = [base[fixture_id] for fixture_id in shared]
    side_coupons = [side[fixture_id] for fixture_id in shared]
    return PairYield(
        baseline_name=baseline.name,
        strategy_name=other.name,
        matches=len(shared),
        baseline_hits=sum(1 for coupon in base_coupons if coupon.status == "won"),
        strategy_hits=sum(1 for coupon in side_coupons if coupon.status == "won"),
        baseline_yield=_yield_of(base_coupons),
        strategy_yield=_yield_of(side_coupons),
    )


def _singles(coupons: Sequence[CouponView]) -> dict[int, CouponView]:
    found: dict[int, CouponView] = {}
    for coupon in coupons:
        leg = _single_played(coupon)
        if leg is None:
            continue
        current = found.get(leg.fixture_id)
        if current is None or coupon.coupon_id > current.coupon_id:
            found[leg.fixture_id] = coupon
    return found


def _single_played(coupon: CouponView) -> LegView | None:
    active = tuple(leg for leg in coupon.legs if leg.status != "removed")
    if len(active) != 1 or coupon.status not in _PLAYED:
        return None
    leg = active[0]
    if leg.status not in _PLAYED or coupon.combined_odd is None:
        return None
    return leg


def _yield_of(coupons: Sequence[CouponView]) -> Decimal | None:
    stakes = sum((coupon.stake for coupon in coupons), _ZERO)
    pnl = sum(
        (
            settled_pnl(
                coupon.stake,
                coupon.combined_odd if coupon.combined_odd is not None else _ZERO,
                Settlement(coupon.status),
                coupon.payout_keep,
            )
            for coupon in coupons
        ),
        _ZERO,
    )
    return _ratio(pnl, stakes)


def _in_period(coupon: CouponView, period: str) -> bool:
    if period == "all":
        return True
    periods = {leg.period for leg in coupon.legs if leg.status != "removed"}
    return periods == {period}


def _ratio(part: Decimal, whole: Decimal) -> Decimal | None:
    if whole == 0:
        return None
    return (part / whole).quantize(_RATE, rounding=ROUND_HALF_UP)


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
