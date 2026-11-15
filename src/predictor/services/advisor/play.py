"""Whether the operator played one sandbox-llm coupon at the bookmaker.

The question is not a graph node. The virtual coupon is already committed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.constants import PAYOUT_KEEP, SANDBOX_LLM_NAME
from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorCouponEvent,
    AdvisorLeg,
    AdvisorPlayClaim,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.models.fixtures import Fixture
from predictor.services.advisor.domain import Period, combined_odd, period_for
from predictor.services.advisor.recommend import (
    inactive_reason,
    league_allowed,
    load_recommendation_setup,
)
from predictor.services.advisor.recommend_ops import (
    ActionError,
    open_recommendation_fixtures,
    recommendation_usage,
)
from predictor.services.advisor.sandbox import LegSnapshot
from predictor.services.advisor.telegram import (
    KIND_PLAY_CLAIM,
    enqueue,
    play_claim_text,
)
from predictor.services.queue import untracked_league_ids

_ODD = Decimal("0.001")
_MONEY = Decimal("0.01")
_RECO_OPEN = "uq_advisor_legs_open_recommendation"


@dataclass(frozen=True, slots=True)
class PlayCounts:
    pending: int
    played: int
    not_played: int

    def skipped_share(self) -> Decimal | None:
        answered = self.played + self.not_played
        if answered == 0:
            return None
        return (Decimal(self.not_played) / Decimal(answered)).quantize(Decimal("0.001"))


def scale_leg_odds(odds: Sequence[Decimal], target: Decimal) -> tuple[Decimal, ...]:
    """Scaled leg prices whose product, to three places, equals ``target``.

    One leg is the target itself. Further legs share one factor, and the
    last leg absorbs the rounding remainder.
    """
    priced = target.quantize(_ODD, rounding=ROUND_HALF_UP)
    if priced <= 1:
        raise ValueError("odd")
    if len(odds) == 1:
        return (priced,)
    base = Decimal(1)
    for odd in odds:
        if odd <= 1:
            raise ValueError("odd")
        base *= odd
    factor = priced / base
    head = [(odd * factor).quantize(_ODD, rounding=ROUND_HALF_UP) for odd in odds[:-1]]
    if any(leg <= 1 for leg in head):
        raise ValueError("odd")
    running = Decimal(1)
    for leg in head:
        running *= leg
    start = (priced / running).quantize(_ODD, rounding=ROUND_HALF_UP)
    for step in range(30):
        signs = (0,) if step == 0 else (1, -1)
        for sign in signs:
            candidate = start + (sign * step * _ODD)
            if candidate <= 1:
                continue
            if combined_odd((*head, candidate)) == priced:
                return (*head, candidate)
    raise ValueError("odd")


async def open_play_claim(
    session: AsyncSession,
    strategy: AdvisorStrategy,
    slip: Sequence[LegSnapshot],
    *,
    coupon_id: int,
    stake: Decimal,
    combined: Decimal,
    lines: tuple[str, ...],
    now: datetime,
) -> bool:
    """Ask only inside the open recommendation window. Otherwise leave the caller."""
    if strategy.name != SANDBOX_LLM_NAME or strategy.status != "promoted":
        return False
    if not await _window_open(session, slip, now):
        return False
    session.add(
        AdvisorPlayClaim(
            sandbox_coupon_id=coupon_id,
            status="pending",
            asked_at=now,
        )
    )
    enqueue(
        session,
        KIND_PLAY_CLAIM,
        play_claim_text(
            coupon_id=coupon_id,
            lines=lines,
            stake=str(stake),
            combined=str(combined),
        ),
    )
    return True


async def mark_not_played(
    session: AsyncSession, coupon_id: int, now: datetime
) -> str:
    """Record a no. A changed score still counts. A settled row stays put."""
    claim = await _claim(session, coupon_id, now)
    if claim.status != "pending":
        return claim.status
    claim.status = "not_played"
    claim.answered_at = now
    return "not_played"


async def mark_played(
    session: AsyncSession,
    coupon_id: int,
    *,
    combined: Decimal,
    stake: Decimal,
    now: datetime,
) -> tuple[str, int | None, bool]:
    """Create one placed coupon. The bool says the price is under the strategy floor."""
    _check_money(combined, stake)
    claim = await _claim(session, coupon_id, now)
    if claim.status == "played":
        return "played", claim.recommendation_coupon_id, False
    if claim.status != "pending":
        raise ActionError("closed")
    coupon = await session.get(AdvisorCoupon, coupon_id)
    if coupon is None or coupon.mode != "sandbox":
        raise ActionError("missing")
    legs = list(
        (
            await session.scalars(
                select(AdvisorLeg).where(AdvisorLeg.coupon_id == coupon.id)
            )
        ).all()
    )
    version = await session.get(AdvisorStrategyVersion, coupon.strategy_version_id)
    if version is None:
        raise ActionError("missing")
    strategy = await session.get(AdvisorStrategy, version.strategy_id)
    if strategy is None or strategy.name != SANDBOX_LLM_NAME:
        raise ActionError("missing")
    snapshots = tuple(
        LegSnapshot(
            fixture_id=leg.fixture_id,
            period=leg.period,
            goals_home=leg.goals_home,
            goals_away=leg.goals_away,
            odd=leg.odd,
            n=leg.n,
        )
        for leg in legs
    )
    if await _score_moved(session, legs):
        raise ActionError("score")
    if not await _window_open(session, snapshots, now):
        raise ActionError("gate")
    scaled = scale_leg_odds(tuple(leg.odd for leg in legs), combined)
    below = combined_odd(scaled) < version.min_combined_odd
    placed_id = await _insert_account_coupon(
        session,
        coupon=coupon,
        legs=legs,
        scaled=scaled,
        strategy_id=strategy.id,
        stake=stake,
        combined=combined.quantize(_ODD, rounding=ROUND_HALF_UP),
        now=now,
    )
    if placed_id is None:
        raise ActionError("conflict")
    claim.status = "played"
    claim.combined_odd = combined.quantize(_ODD, rounding=ROUND_HALF_UP)
    claim.stake = stake.quantize(_MONEY, rounding=ROUND_HALF_UP)
    claim.recommendation_coupon_id = placed_id
    claim.answered_at = now
    return "played", placed_id, below


async def load_play_counts(session: AsyncSession) -> PlayCounts | None:
    strategy_id = await session.scalar(
        select(AdvisorStrategy.id).where(AdvisorStrategy.name == SANDBOX_LLM_NAME)
    )
    if strategy_id is None:
        return None
    rows = (
        await session.execute(
            select(AdvisorPlayClaim.status)
            .join(AdvisorCoupon, AdvisorCoupon.id == AdvisorPlayClaim.sandbox_coupon_id)
            .join(
                AdvisorStrategyVersion,
                AdvisorStrategyVersion.id == AdvisorCoupon.strategy_version_id,
            )
            .where(AdvisorStrategyVersion.strategy_id == strategy_id)
        )
    ).all()
    counts = {"pending": 0, "played": 0, "not_played": 0}
    for (status,) in rows:
        if status in counts:
            counts[status] += 1
    return PlayCounts(**counts)


def _check_money(combined: Decimal, stake: Decimal) -> None:
    if combined <= 1 or combined != combined.quantize(_ODD):
        raise ActionError("odd")
    if stake <= 0 or stake != stake.quantize(_MONEY):
        raise ActionError("stake")


async def _claim(
    session: AsyncSession, coupon_id: int, now: datetime
) -> AdvisorPlayClaim:
    claim = await session.scalar(
        select(AdvisorPlayClaim)
        .where(AdvisorPlayClaim.sandbox_coupon_id == coupon_id)
        .with_for_update()
    )
    if claim is not None:
        return claim
    coupon = await session.get(AdvisorCoupon, coupon_id)
    if coupon is None or coupon.mode != "sandbox":
        raise ActionError("missing")
    version = await session.get(AdvisorStrategyVersion, coupon.strategy_version_id)
    strategy = None
    if version is not None:
        strategy = await session.get(AdvisorStrategy, version.strategy_id)
    if strategy is None or strategy.name != SANDBOX_LLM_NAME:
        raise ActionError("missing")
    claim = AdvisorPlayClaim(
        sandbox_coupon_id=coupon_id,
        status="pending",
        asked_at=now,
    )
    session.add(claim)
    await session.flush()
    return claim


async def _window_open(
    session: AsyncSession, slip: Sequence[LegSnapshot], now: datetime
) -> bool:
    setup = load_recommendation_setup()
    coupons_today, loss_today = await recommendation_usage(session, now)
    if inactive_reason(setup, now, coupons_today=coupons_today, loss_today=loss_today):
        return False
    fixture_ids = [leg.fixture_id for leg in slip]
    rows = (
        await session.execute(
            select(Fixture.id, Fixture.league_id).where(Fixture.id.in_(fixture_ids))
        )
    ).all()
    leagues = {int(fixture_id): int(league_id) for fixture_id, league_id in rows}
    ignored = frozenset(await untracked_league_ids(session))
    if any(fixture_id not in leagues for fixture_id in fixture_ids):
        return False
    if any(
        not league_allowed(leagues[leg.fixture_id], setup, untracked=ignored)
        for leg in slip
    ):
        return False
    busy = await open_recommendation_fixtures(session)
    return all(leg.fixture_id not in busy for leg in slip)


async def _score_moved(session: AsyncSession, legs: Sequence[AdvisorLeg]) -> bool:
    rows = (
        await session.scalars(
            select(Fixture).where(Fixture.id.in_([leg.fixture_id for leg in legs]))
        )
    ).all()
    found = {int(row.id): row for row in rows}
    for leg in legs:
        fixture = found.get(leg.fixture_id)
        if fixture is None or fixture.goals_home is None or fixture.goals_away is None:
            return True
        period = period_for(fixture.status_short or "")
        if period is None or period is not Period(leg.period):
            return True
        if fixture.goals_home != leg.goals_home or fixture.goals_away != leg.goals_away:
            return True
    return False


async def _insert_account_coupon(
    session: AsyncSession,
    *,
    coupon: AdvisorCoupon,
    legs: Sequence[AdvisorLeg],
    scaled: Sequence[Decimal],
    strategy_id: int,
    stake: Decimal,
    combined: Decimal,
    now: datetime,
) -> int | None:
    try:
        async with session.begin_nested():
            placed = AdvisorCoupon(
                mode="recommendation",
                strategy_version_id=coupon.strategy_version_id,
                cycle_id=coupon.cycle_id,
                status="placed",
                stake=stake.quantize(_MONEY, rounding=ROUND_HALF_UP),
                proposed_stake=coupon.stake,
                combined_odd=combined,
                proposed_combined_odd=coupon.combined_odd,
                payout_keep=PAYOUT_KEEP,
                placed_at=now,
                created_at=now,
            )
            session.add(placed)
            await session.flush()
            for leg, odd in zip(legs, scaled, strict=True):
                session.add(
                    AdvisorLeg(
                        coupon_id=placed.id,
                        strategy_id=strategy_id,
                        fixture_id=leg.fixture_id,
                        mode="recommendation",
                        period=leg.period,
                        n=leg.n,
                        odd=odd,
                        proposed_odd=leg.odd,
                        goals_home=leg.goals_home,
                        goals_away=leg.goals_away,
                        status="open",
                    )
                )
            await session.flush()
    except IntegrityError as exc:
        if _recommendation_conflict(exc):
            return None
        raise
    session.add(
        AdvisorCouponEvent(
            coupon_id=placed.id,
            previous_status="none",
            new_status="placed",
            source="operator",
            note="",
        )
    )
    return int(placed.id)


def _recommendation_conflict(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    return getattr(diag, "constraint_name", None) == _RECO_OPEN
