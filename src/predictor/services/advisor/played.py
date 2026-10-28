"""Played coupons for the operator list. Read-only; does not settle."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorLeg,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.models.catalog import League
from predictor.models.fixtures import Fixture, Team
from predictor.services.advisor.domain import (
    LiveProgress,
    LiveScore,
    Period,
    live_coupon_progress,
    live_leg_progress,
    period_score_now,
)

KindFilter = Literal["all", "baseline", "llm"]

_SANDBOX_STATUSES = frozenset({"open", "won", "lost", "void"})
_RECO_STATUSES = frozenset({"placed", "won", "lost", "void"})
_KINDS = frozenset({"all", "baseline", "llm"})
_DEFAULT_LIMIT = 50
_MIN_LIMIT = 1
_MAX_LIMIT = 200


@dataclass(frozen=True, slots=True)
class PlayedLegView:
    fixture_id: int
    home: str
    away: str
    home_logo: str | None
    away_logo: str | None
    league: str | None
    country: str | None
    period: str
    n: int
    odd: Decimal
    score_at_bet: tuple[int, int]
    score_now: tuple[int, int] | None
    status_short: str | None
    elapsed: int | None
    progress: LiveProgress


@dataclass(frozen=True, slots=True)
class PlayedCouponView:
    id: int
    placed_at: datetime | None
    kind: str
    strategy: str
    mode: str
    status: str
    stake: Decimal
    combined_odd: Decimal | None
    legs: tuple[PlayedLegView, ...]
    progress: LiveProgress


def parse_played_kind(raw: str) -> KindFilter:
    if raw not in _KINDS:
        raise ValueError("invalid_kind")
    return raw  # type: ignore[return-value]


def parse_played_limit(raw: int) -> int:
    if raw < _MIN_LIMIT or raw > _MAX_LIMIT:
        raise ValueError("invalid_limit")
    return raw


async def load_played_coupons(
    session: AsyncSession,
    *,
    kind: KindFilter = "all",
    limit: int = _DEFAULT_LIMIT,
) -> tuple[PlayedCouponView, ...]:
    """Sandbox open/won/lost/void plus confirmed recommendation coupons.

    Callers must pass a validated ``kind`` and ``limit`` (see parse helpers).
    """
    home = aliased(Team)
    away = aliased(Team)
    sandbox = and_(
        AdvisorCoupon.mode == "sandbox",
        AdvisorStrategy.kind.in_(("baseline", "llm")),
        AdvisorCoupon.status.in_(_SANDBOX_STATUSES),
    )
    recommendation = and_(
        AdvisorCoupon.mode == "recommendation",
        AdvisorCoupon.status.in_(_RECO_STATUSES),
    )
    filters = [or_(sandbox, recommendation)]
    if kind != "all":
        filters.append(AdvisorStrategy.kind == kind)
    ranked = (
        select(
            AdvisorCoupon.id.label("coupon_id"),
            AdvisorCoupon.placed_at,
            AdvisorCoupon.created_at,
        )
        .join(
            AdvisorStrategyVersion,
            AdvisorStrategyVersion.id == AdvisorCoupon.strategy_version_id,
        )
        .join(
            AdvisorStrategy,
            AdvisorStrategy.id == AdvisorStrategyVersion.strategy_id,
        )
        .where(*filters)
        .order_by(
            AdvisorCoupon.placed_at.desc().nulls_last(),
            AdvisorCoupon.created_at.desc(),
            AdvisorCoupon.id.desc(),
        )
        .limit(limit)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                AdvisorCoupon,
                AdvisorStrategy,
                AdvisorLeg,
                Fixture,
                home.name,
                away.name,
                home.logo,
                away.logo,
                League.name,
                League.country_name,
                ranked.c.placed_at,
                ranked.c.created_at,
            )
            .join(ranked, ranked.c.coupon_id == AdvisorCoupon.id)
            .join(
                AdvisorStrategyVersion,
                AdvisorStrategyVersion.id == AdvisorCoupon.strategy_version_id,
            )
            .join(
                AdvisorStrategy,
                AdvisorStrategy.id == AdvisorStrategyVersion.strategy_id,
            )
            .join(AdvisorLeg, AdvisorLeg.coupon_id == AdvisorCoupon.id)
            .join(Fixture, Fixture.id == AdvisorLeg.fixture_id)
            .join(home, home.id == Fixture.home_team_id)
            .join(away, away.id == Fixture.away_team_id)
            .outerjoin(League, League.id == Fixture.league_id)
            .order_by(
                ranked.c.placed_at.desc().nulls_last(),
                ranked.c.created_at.desc(),
                AdvisorCoupon.id.desc(),
                AdvisorLeg.id,
            )
        )
    ).all()
    legs_by_coupon: dict[int, list[PlayedLegView]] = defaultdict(list)
    owners: dict[int, tuple[AdvisorCoupon, AdvisorStrategy]] = {}
    order: list[int] = []
    for (
        coupon,
        strategy,
        leg,
        fixture,
        home_name,
        away_name,
        home_logo,
        away_logo,
        league_name,
        country_name,
        _placed,
        _created,
    ) in rows:
        if coupon.id not in owners:
            owners[coupon.id] = (coupon, strategy)
            order.append(coupon.id)
        score = LiveScore(
            status_short=str(fixture.status_short or ""),
            goals_home=fixture.goals_home,
            goals_away=fixture.goals_away,
            fulltime_home=fixture.goals_home_fulltime,
            fulltime_away=fixture.goals_away_fulltime,
            extratime_home=fixture.goals_home_extratime,
            extratime_away=fixture.goals_away_extratime,
        )
        period = Period(leg.period)
        progress = live_leg_progress(period, leg.n, score)
        legs_by_coupon[coupon.id].append(
            PlayedLegView(
                fixture_id=leg.fixture_id,
                home=str(home_name or "?"),
                away=str(away_name or "?"),
                home_logo=None if home_logo is None else str(home_logo),
                away_logo=None if away_logo is None else str(away_logo),
                league=None if league_name is None else str(league_name),
                country=None if country_name is None else str(country_name),
                period=leg.period,
                n=leg.n,
                odd=leg.odd,
                score_at_bet=(leg.goals_home, leg.goals_away),
                score_now=period_score_now(period, score),
                status_short=fixture.status_short,
                elapsed=fixture.elapsed_minutes,
                progress=progress,
            )
        )
    views: list[PlayedCouponView] = []
    for coupon_id in order:
        coupon, strategy = owners[coupon_id]
        legs = tuple(legs_by_coupon.get(coupon_id, ()))
        if not legs:
            continue
        views.append(
            PlayedCouponView(
                id=coupon.id,
                placed_at=coupon.placed_at or coupon.created_at,
                kind=strategy.kind,
                strategy=strategy.name,
                mode=coupon.mode,
                status=coupon.status,
                stake=coupon.stake,
                combined_odd=coupon.combined_odd,
                legs=legs,
                progress=live_coupon_progress(tuple(leg.progress for leg in legs)),
            )
        )
    return tuple(views)


def played_coupons_as_dict(
    coupons: Sequence[PlayedCouponView],
) -> dict[str, Any]:
    return {
        "coupons": [_coupon_dict(item) for item in coupons],
    }


def _coupon_dict(coupon: PlayedCouponView) -> dict[str, Any]:
    return {
        "id": coupon.id,
        "placed_at": None if coupon.placed_at is None else coupon.placed_at.isoformat(),
        "kind": coupon.kind,
        "strategy": coupon.strategy,
        "mode": coupon.mode,
        "status": coupon.status,
        "stake": _text(coupon.stake),
        "combined_odd": _text(coupon.combined_odd),
        "progress": coupon.progress.value,
        "legs": [_leg_dict(leg) for leg in coupon.legs],
    }


def _leg_dict(leg: PlayedLegView) -> dict[str, Any]:
    return {
        "fixture_id": leg.fixture_id,
        "home": leg.home,
        "away": leg.away,
        "league": leg.league,
        "period": leg.period,
        "n": leg.n,
        "odd": _text(leg.odd),
        "score_at_bet": _score_text(leg.score_at_bet),
        "score_now": None if leg.score_now is None else _score_text(leg.score_now),
        "status_short": leg.status_short,
        "elapsed": leg.elapsed,
        "progress": leg.progress.value,
    }


def _score_text(score: tuple[int, int]) -> str:
    return f"{score[0]}-{score[1]}"


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
