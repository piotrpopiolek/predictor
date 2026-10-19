"""Recommendation coupons and strategy status changes. GET does not call this."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.constants import PAYOUT_KEEP
from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorCouponEvent,
    AdvisorLeg,
    AdvisorSettlementEvent,
    AdvisorStrategy,
    AdvisorStrategyEvent,
    AdvisorStrategyVersion,
)
from predictor.models.fixtures import Fixture
from predictor.services.advisor.domain import (
    Period,
    Settlement,
    combined_odd,
    period_for,
    settled_pnl,
)
from predictor.services.advisor.recommend import (
    max_drawdown,
    net_yield,
    promotion_block,
    withdraw_due,
)
from predictor.services.advisor.sandbox import SandboxSlip
from predictor.services.advisor.telegram import enqueue, recommendation_text

_LIVE = frozenset({"proposed", "accepted", "modified"})
_CONFIRM = frozenset({"accepted", "modified"})
_RECO_OPEN = "uq_advisor_legs_open_recommendation"
_MONEY = Decimal("0.01")


class ActionError(Exception):
    """Operator/action failure. ``commit`` keeps intentional expiry writes."""

    def __init__(self, code: str, *, commit: bool = False) -> None:
        self.code = code
        self.commit = commit
        super().__init__(code)


async def accept_coupon(
    session: AsyncSession, coupon_id: int, now: datetime, ttl_seconds: int = 180
) -> None:
    coupon = await _live(session, coupon_id, now, ttl_seconds)
    if coupon.status != "proposed":
        raise ActionError("not_proposed")
    _move(session, coupon, "accepted", "operator", "")


async def reject_coupon(
    session: AsyncSession,
    coupon_id: int,
    now: datetime,
    note: str,
    ttl_seconds: int = 180,
) -> None:
    coupon = await _live(session, coupon_id, now, ttl_seconds)
    if coupon.status not in _LIVE:
        raise ActionError("closed")
    await _release_legs(session, coupon.id)
    _move(session, coupon, "rejected", "operator", note.strip())


async def mark_not_placed(session: AsyncSession, coupon_id: int, now: datetime) -> None:
    coupon = await _lock(session, coupon_id)
    if coupon.mode != "recommendation" or coupon.status not in _CONFIRM:
        raise ActionError("not_confirmable")
    await _release_legs(session, coupon.id)
    _move(session, coupon, "not_placed", "operator", "")


async def modify_coupon(
    session: AsyncSession,
    coupon_id: int,
    now: datetime,
    *,
    stake: Decimal,
    odds: dict[int, Decimal],
    remove: set[int],
    ttl_seconds: int = 180,
) -> bool:
    """Return whether the corrected coupon is under the strategy odd floor."""
    if stake <= 0 or stake != stake.quantize(_MONEY):
        raise ActionError("stake")
    coupon = await _live(session, coupon_id, now, ttl_seconds)
    if coupon.status not in _LIVE:
        raise ActionError("closed")
    legs = await _legs(session, coupon.id)
    priced_odds: list[Decimal] = []
    changes: list[tuple[AdvisorLeg, Decimal | None]] = []
    for leg in legs:
        if leg.status == "removed":
            continue
        if leg.fixture_id in remove:
            changes.append((leg, None))
            continue
        if leg.fixture_id not in odds:
            raise ActionError("odd")
        odd = odds[leg.fixture_id]
        if odd <= 1:
            raise ActionError("odd")
        changes.append((leg, odd))
        priced_odds.append(odd)
    if not priced_odds:
        raise ActionError("empty")
    priced = combined_odd(tuple(priced_odds))
    for leg, next_odd in changes:
        if next_odd is None:
            leg.status = "removed"
        else:
            leg.odd = next_odd
    if coupon.proposed_stake is None:
        coupon.proposed_stake = coupon.stake
    coupon.stake = stake
    coupon.combined_odd = priced
    version = await session.get(AdvisorStrategyVersion, coupon.strategy_version_id)
    floor = version.min_combined_odd if version is not None else Decimal("1.600")
    _move(session, coupon, "modified", "operator", "")
    return priced < floor


async def place_coupon(
    session: AsyncSession,
    coupon_id: int,
    now: datetime,
    ttl_seconds: int = 180,
) -> None:
    coupon = await _lock(session, coupon_id)
    if coupon.mode != "recommendation":
        raise ActionError("not_recommendation")
    if coupon.status == "expired":
        raise ActionError("expired")
    if coupon.status not in _CONFIRM:
        raise ActionError("not_confirmable")
    if now - coupon.created_at >= timedelta(seconds=ttl_seconds):
        await _expire_locked(session, coupon, "expire", "ttl")
        raise ActionError("expired", commit=True)
    legs = [leg for leg in await _legs(session, coupon.id) if leg.status != "removed"]
    fixtures = await _fixtures(session, [leg.fixture_id for leg in legs])
    if _score_changed(legs, fixtures):
        await _expire_locked(session, coupon, "expire", "score")
        raise ActionError("expired", commit=True)
    coupon.placed_at = now
    _move(session, coupon, "placed", "operator", "")


async def expire_due(session: AsyncSession, now: datetime, ttl_seconds: int) -> int:
    """Expire recommendations whose clock or score is no longer the proposal."""
    rows = (
        await session.scalars(
            select(AdvisorCoupon)
            .where(AdvisorCoupon.mode == "recommendation")
            .where(AdvisorCoupon.status.in_(_LIVE))
            .order_by(AdvisorCoupon.id)
            .with_for_update()
        )
    ).all()
    expired = 0
    limit = timedelta(seconds=ttl_seconds)
    for coupon in rows:
        if coupon.status not in _LIVE:
            continue
        legs = [
            leg for leg in await _legs(session, coupon.id) if leg.status != "removed"
        ]
        fixtures = await _fixtures(session, [leg.fixture_id for leg in legs])
        stale = now - coupon.created_at >= limit or _score_changed(legs, fixtures)
        if not stale:
            continue
        await _expire_locked(session, coupon, "expire", "")
        expired += 1
    return expired


async def place_proposed(
    session: AsyncSession,
    *,
    strategy_id: int,
    strategy_version_id: int,
    slips: Sequence[SandboxSlip],
    cycle_id: int | None,
    stake: Decimal,
    created_at: datetime,
) -> tuple[int, ...]:
    """One proposed coupon per slip. A fixture already open is skipped."""
    placed: list[int] = []
    for slip in slips:
        odd = combined_odd(tuple(leg.odd for leg in slip.legs))
        try:
            async with session.begin_nested():
                coupon = AdvisorCoupon(
                    mode="recommendation",
                    strategy_version_id=strategy_version_id,
                    cycle_id=cycle_id,
                    status="proposed",
                    stake=stake,
                    proposed_stake=stake,
                    combined_odd=odd,
                    proposed_combined_odd=odd,
                    payout_keep=PAYOUT_KEEP,
                    placed_at=None,
                    created_at=created_at,
                )
                session.add(coupon)
                await session.flush()
                for leg in slip.legs:
                    session.add(
                        AdvisorLeg(
                            coupon_id=coupon.id,
                            strategy_id=strategy_id,
                            fixture_id=leg.fixture_id,
                            mode="recommendation",
                            period=leg.period,
                            n=leg.n,
                            odd=leg.odd,
                            proposed_odd=leg.odd,
                            goals_home=leg.goals_home,
                            goals_away=leg.goals_away,
                            status="open",
                        )
                    )
                await session.flush()
        except IntegrityError as exc:
            if _recommendation_conflict(exc):
                continue
            raise
        placed.append(coupon.id)
        session.add(
            AdvisorCouponEvent(
                coupon_id=coupon.id,
                previous_status="none",
                new_status="proposed",
                source="operator",
                note="",
            )
        )
        lines = tuple(
            f"mecz {leg.fixture_id} Over {leg.n}+0.5 kurs {leg.odd}"
            for leg in slip.legs
        )
        enqueue(
            session,
            "recommendation",
            recommendation_text(
                coupon_id=coupon.id,
                lines=lines,
                stake=str(stake),
                combined=str(odd),
                expires=created_at.isoformat(),
                link=f"/advisor/recommendations/{coupon.id}",
            ),
        )
    return tuple(placed)


async def promote_strategy(session: AsyncSession, strategy_id: int) -> None:
    strategy = await _strategy(session, strategy_id)
    if strategy.kind != "llm" or strategy.status == "retired":
        raise ActionError("retired")
    book = await _sandbox_book(session, strategy.id)
    baseline = await _baseline_yield(session)
    block = promotion_block(
        settled=book[0],
        yield_net=book[1],
        baseline_yield=baseline,
        drawdown=book[2],
    )
    if block is not None:
        raise ActionError(block)
    current = await session.scalar(
        select(AdvisorStrategy)
        .where(AdvisorStrategy.status == "promoted")
        .with_for_update()
    )
    if current is not None and current.id != strategy.id:
        _strategy_move(session, current, "sandbox", "operator", "replaced")
    _strategy_move(session, strategy, "promoted", "operator", "")


async def set_strategy_status(
    session: AsyncSession, strategy_id: int, status: str
) -> None:
    strategy = await _strategy(session, strategy_id)
    if strategy.status == "retired":
        raise ActionError("retired")
    if status not in {"sandbox", "paused", "retired"}:
        raise ActionError("status")
    if status == "sandbox" and strategy.status != "paused":
        raise ActionError("status")
    _strategy_move(session, strategy, status, "operator", "")


async def withdraw_promoted(session: AsyncSession, floor: Decimal) -> str | None:
    """Return a telegram line when the promoted strategy falls under the floor."""
    strategy = await session.scalar(
        select(AdvisorStrategy)
        .where(AdvisorStrategy.status == "promoted")
        .with_for_update()
    )
    if strategy is None:
        return None
    yield_net = (await _sandbox_book(session, strategy.id, limit=100))[1]
    if not withdraw_due(yield_net, floor):
        return None
    _strategy_move(session, strategy, "sandbox", "withdraw", "")
    coupons = (
        await session.scalars(
            select(AdvisorCoupon)
            .join(
                AdvisorStrategyVersion,
                AdvisorStrategyVersion.id == AdvisorCoupon.strategy_version_id,
            )
            .where(AdvisorStrategyVersion.strategy_id == strategy.id)
            .where(AdvisorCoupon.mode == "recommendation")
            .where(AdvisorCoupon.status.in_(_LIVE))
            .order_by(AdvisorCoupon.id)
            .with_for_update()
        )
    ).all()
    for coupon in coupons:
        if coupon.status not in _LIVE:
            continue
        await _expire_locked(session, coupon, "withdraw", "")
    shown = "brak" if yield_net is None else str(yield_net)
    return f"Wycofano {strategy.name}. Yield {shown}, próg {floor}."


async def launch_proposed(
    session: AsyncSession, strategy_id: int, *, limit: int
) -> None:
    strategy = await _strategy(session, strategy_id)
    if strategy.status != "proposed" or strategy.kind != "llm":
        raise ActionError("status")
    active = await session.scalar(
        select(func.count())
        .select_from(AdvisorStrategy)
        .where(AdvisorStrategy.kind == "llm")
        .where(AdvisorStrategy.status.in_(("sandbox", "promoted")))
    )
    if int(active or 0) >= limit:
        raise ActionError("strategy_limit")
    _strategy_move(session, strategy, "sandbox", "operator", "launch")


async def manual_settle(session: AsyncSession, leg_id: int, outcome: str) -> None:
    if outcome not in {"won", "lost", "void"}:
        raise ActionError("outcome")
    leg = await session.scalar(
        select(AdvisorLeg).where(AdvisorLeg.id == leg_id).with_for_update()
    )
    if leg is None or leg.status != "manual":
        raise ActionError("not_manual")
    previous = leg.status
    leg.status = outcome
    session.add(
        AdvisorSettlementEvent(
            coupon_id=leg.coupon_id,
            leg_id=leg.id,
            previous_status=previous,
            new_status=outcome,
            source="manual",
        )
    )


async def recommendation_usage(
    session: AsyncSession, now: datetime
) -> tuple[int, Decimal]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    count = await session.scalar(
        select(func.count())
        .select_from(AdvisorCoupon)
        .where(AdvisorCoupon.mode == "recommendation")
        .where(AdvisorCoupon.created_at >= start)
        .where(AdvisorCoupon.status.notin_(("rejected", "expired")))
    )
    rows = (
        await session.execute(
            select(
                AdvisorCoupon.stake,
                AdvisorCoupon.combined_odd,
                AdvisorCoupon.payout_keep,
                AdvisorCoupon.status,
            )
            .where(AdvisorCoupon.mode == "recommendation")
            .where(AdvisorCoupon.placed_at >= start)
            .where(AdvisorCoupon.status.in_(("won", "lost")))
        )
    ).all()
    net = sum(
        (
            settled_pnl(stake, odd, Settlement(status), keep)
            for stake, odd, keep, status in rows
            if odd is not None
        ),
        Decimal("0"),
    )
    loss = -net if net < 0 else Decimal("0")
    return int(count or 0), loss


async def open_recommendation_fixtures(session: AsyncSession) -> frozenset[int]:
    rows = await session.scalars(
        select(AdvisorLeg.fixture_id)
        .join(AdvisorCoupon, AdvisorCoupon.id == AdvisorLeg.coupon_id)
        .where(AdvisorLeg.mode == "recommendation")
        .where(AdvisorLeg.status.in_(("open", "manual")))
        .where(AdvisorCoupon.status.in_(_LIVE | frozenset({"placed"})))
    )
    return frozenset(int(fixture_id) for fixture_id in rows)


def _move(
    session: AsyncSession,
    coupon: AdvisorCoupon,
    status: str,
    source: str,
    note: str,
) -> None:
    if coupon.status == status and status != "proposed":
        return
    previous = coupon.status
    coupon.status = status
    session.add(
        AdvisorCouponEvent(
            coupon_id=coupon.id,
            previous_status=previous,
            new_status=status,
            source=source,
            note=note,
        )
    )


def _strategy_move(
    session: AsyncSession,
    strategy: AdvisorStrategy,
    status: str,
    source: str,
    note: str,
) -> None:
    previous = strategy.status
    if previous == status:
        return
    strategy.status = status
    session.add(
        AdvisorStrategyEvent(
            strategy_id=strategy.id,
            previous_status=previous,
            new_status=status,
            source=source,
            note=note,
        )
    )


async def _live(
    session: AsyncSession, coupon_id: int, now: datetime, ttl_seconds: int
) -> AdvisorCoupon:
    coupon = await _lock(session, coupon_id)
    if coupon.mode != "recommendation":
        raise ActionError("not_recommendation")
    if coupon.status == "expired":
        raise ActionError("expired")
    if now - coupon.created_at >= timedelta(seconds=ttl_seconds):
        if coupon.status in _LIVE:
            await _expire_locked(session, coupon, "expire", "ttl")
        raise ActionError("expired", commit=True)
    return coupon


async def _expire_locked(
    session: AsyncSession, coupon: AdvisorCoupon, source: str, note: str
) -> None:
    await _release_legs(session, coupon.id)
    _move(session, coupon, "expired", source, note)


async def _lock(session: AsyncSession, coupon_id: int) -> AdvisorCoupon:
    coupon = await session.scalar(
        select(AdvisorCoupon).where(AdvisorCoupon.id == coupon_id).with_for_update()
    )
    if coupon is None:
        raise ActionError("missing")
    return coupon


async def _strategy(session: AsyncSession, strategy_id: int) -> AdvisorStrategy:
    strategy = await session.scalar(
        select(AdvisorStrategy)
        .where(AdvisorStrategy.id == strategy_id)
        .with_for_update()
    )
    if strategy is None:
        raise ActionError("missing")
    return strategy


async def _legs(session: AsyncSession, coupon_id: int) -> list[AdvisorLeg]:
    return list(
        (
            await session.scalars(
                select(AdvisorLeg).where(AdvisorLeg.coupon_id == coupon_id)
            )
        ).all()
    )


async def _release_legs(session: AsyncSession, coupon_id: int) -> None:
    for leg in await _legs(session, coupon_id):
        if leg.status == "open":
            leg.status = "removed"


async def _fixtures(
    session: AsyncSession, fixture_ids: list[int]
) -> dict[int, Fixture]:
    if not fixture_ids:
        return {}
    rows = (
        await session.scalars(select(Fixture).where(Fixture.id.in_(fixture_ids)))
    ).all()
    return {int(row.id): row for row in rows}


def _score_changed(legs: Sequence[AdvisorLeg], fixtures: dict[int, Fixture]) -> bool:
    for leg in legs:
        fixture = fixtures.get(leg.fixture_id)
        if fixture is None or fixture.goals_home is None or fixture.goals_away is None:
            return True
        period = period_for(fixture.status_short or "")
        if period is None or period is not Period(leg.period):
            return True
        if fixture.goals_home != leg.goals_home or fixture.goals_away != leg.goals_away:
            return True
    return False


def _recommendation_conflict(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    return getattr(diag, "constraint_name", None) == _RECO_OPEN


async def _sandbox_book(
    session: AsyncSession, strategy_id: int, *, limit: int | None = None
) -> tuple[int, Decimal | None, Decimal]:
    query = (
        select(
            AdvisorCoupon.stake,
            AdvisorCoupon.combined_odd,
            AdvisorCoupon.payout_keep,
            AdvisorCoupon.status,
        )
        .join(
            AdvisorStrategyVersion,
            AdvisorStrategyVersion.id == AdvisorCoupon.strategy_version_id,
        )
        .where(AdvisorStrategyVersion.strategy_id == strategy_id)
        .where(AdvisorCoupon.mode == "sandbox")
        .where(AdvisorCoupon.status.in_(("won", "lost")))
        .order_by(AdvisorCoupon.id.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    rows = list((await session.execute(query)).all())
    rows.reverse()
    pnls: list[Decimal] = []
    stakes = Decimal("0")
    for stake, odd, keep, status in rows:
        if odd is None:
            continue
        pnls.append(settled_pnl(stake, odd, Settlement(status), keep))
        stakes += stake
    pnl = sum(pnls, Decimal("0"))
    return len(pnls), net_yield(pnl, stakes), max_drawdown(pnls)


async def _baseline_yield(session: AsyncSession) -> Decimal | None:
    strategy = await session.scalar(
        select(AdvisorStrategy)
        .where(AdvisorStrategy.kind == "baseline")
        .order_by(AdvisorStrategy.id)
        .limit(1)
    )
    if strategy is None:
        return None
    return (await _sandbox_book(session, strategy.id))[1]
