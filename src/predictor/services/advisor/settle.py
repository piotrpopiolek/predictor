"""Settle sandbox coupons after the match is final. Idempotent.

A finished score counts only after the fixture detail task is complete.
That is the first post-match refresh. A later change inside 24 hours
rewrites the leg and writes an audit row. Telegram is a later step.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.constants import CONTROL_REFRESH_HOURS, FINISHED_FIXTURE_STATUSES
from predictor.models.advisor import (
    AdvisorCounterfactual,
    AdvisorCoupon,
    AdvisorDecision,
    AdvisorLeg,
    AdvisorSettlementEvent,
)
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.services.advisor.domain import (
    FinalScore,
    Period,
    Settlement,
    active_combined_odd,
    settle_coupon,
    settle_leg,
)

_OPEN_LEG = ("open", "won", "lost", "void", "manual")
# Sandbox is open at placement. Recommendation waits for placed.
_COUPON = ("open", "placed", "won", "lost", "void")
_FIRST_SETTLE = ("open", "placed")
_COUNTED = ("bet", "reject", "wait_timeout")
# Enrichment ends complete when children/coverage exist; coverage_empty when
# the FT detail row was stored but the league has no events/lineups/stats.
_DETAIL_REFRESHED = frozenset({"complete", "coverage_empty"})
_TERMINAL = {
    Settlement.WON: "won",
    Settlement.LOST: "lost",
    Settlement.VOID: "void",
    Settlement.MANUAL: "manual",
}


async def settle_finished(session: AsyncSession, now: datetime) -> int:
    """Update finished legs and coupons. Returns how many coupons changed."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    rows = (
        await session.execute(
            select(AdvisorLeg, Fixture, AdvisorCoupon)
            .join(Fixture, Fixture.id == AdvisorLeg.fixture_id)
            .join(AdvisorCoupon, AdvisorCoupon.id == AdvisorLeg.coupon_id)
            .where(AdvisorLeg.status.in_(_OPEN_LEG))
            .where(AdvisorCoupon.status.in_(_COUPON))
        )
    ).all()
    fixture_ids = {int(fx.id) for _, fx, _ in rows}
    refreshed = await _refreshed(session, fixture_ids)
    touched: set[int] = set()
    for leg, fixture, _coupon in rows:
        new = settle_leg(
            Period(leg.period),
            leg.n,
            _final_score(fixture, refreshed, now),
        )
        name = _TERMINAL.get(new)
        if name is None or name == leg.status:
            continue
        if leg.status != "open" and not _inside_correction_window(fixture, now):
            continue
        source = "settle" if leg.status == "open" else "correction"
        previous = leg.status
        leg.status = name
        session.add(
            AdvisorSettlementEvent(
                coupon_id=leg.coupon_id,
                leg_id=leg.id,
                previous_status=previous,
                new_status=name,
                source=source,
            )
        )
        touched.add(leg.coupon_id)
    changed = 0
    for coupon_id in touched:
        if await _refresh_coupon(session, coupon_id):
            changed += 1
    await _record_counterfactuals(session, now)
    return changed


async def _refreshed(session: AsyncSession, fixture_ids: set[int]) -> set[int]:
    if not fixture_ids:
        return set()
    rows = await session.scalars(
        select(EtlTask.fixture_id)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.fixture_id.in_(fixture_ids))
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.status.in_(_DETAIL_REFRESHED))
    )
    return {int(fixture_id) for fixture_id in rows if fixture_id is not None}


def _final_score(fixture: Fixture, refreshed: set[int], now: datetime) -> FinalScore:
    status = fixture.status_short or ""
    kick = fixture.date
    waiting = False
    if status not in FINISHED_FIXTURE_STATUSES and kick is not None:
        if kick.tzinfo is None:
            kick = kick.replace(tzinfo=UTC)
        waiting = now - kick >= timedelta(hours=CONTROL_REFRESH_HOURS)
    return FinalScore(
        status_short=status,
        fulltime_home=fixture.goals_home_fulltime,
        fulltime_away=fixture.goals_away_fulltime,
        extratime_home=fixture.goals_home_extratime,
        extratime_away=fixture.goals_away_extratime,
        final_refreshed=fixture.id in refreshed,
        awaiting_manual=waiting,
    )


def _inside_correction_window(fixture: Fixture, now: datetime) -> bool:
    kick = fixture.date
    if kick is None:
        return True
    if kick.tzinfo is None:
        kick = kick.replace(tzinfo=UTC)
    return now - kick <= timedelta(hours=CONTROL_REFRESH_HOURS)


async def _refresh_coupon(session: AsyncSession, coupon_id: int) -> bool:
    coupon = await session.get(AdvisorCoupon, coupon_id)
    if coupon is None:
        return False
    legs = (
        await session.scalars(
            select(AdvisorLeg).where(AdvisorLeg.coupon_id == coupon_id)
        )
    ).all()
    result = settle_coupon(tuple(_as_settlement(leg.status) for leg in legs))
    if result not in (Settlement.WON, Settlement.LOST, Settlement.VOID):
        return False
    new_status = result.value
    new_odd = coupon.combined_odd
    if result is Settlement.WON:
        priced = [(leg.odd, _as_settlement(leg.status)) for leg in legs]
        active = active_combined_odd(priced)
        if active is not None:
            new_odd = active
    if coupon.status == new_status and coupon.combined_odd == new_odd:
        return False
    source = "settle" if coupon.status in _FIRST_SETTLE else "correction"
    previous = coupon.status
    coupon.status = new_status
    coupon.combined_odd = new_odd
    session.add(
        AdvisorSettlementEvent(
            coupon_id=coupon.id,
            leg_id=None,
            previous_status=previous,
            new_status=new_status,
            source=source,
        )
    )
    return True


def _as_settlement(status: str) -> Settlement:
    if status == "won":
        return Settlement.WON
    if status == "lost":
        return Settlement.LOST
    # A removed leg is out of the coupon, so it drops out of the product.
    if status in ("void", "removed"):
        return Settlement.VOID
    if status == "manual":
        return Settlement.MANUAL
    return Settlement.PENDING


async def _record_counterfactuals(session: AsyncSession, now: datetime) -> None:
    """Record whether a later goal arrived. A reject with no coupon still counts."""
    missing = ~exists(
        select(AdvisorCounterfactual.id).where(
            AdvisorCounterfactual.decision_id == AdvisorDecision.id
        )
    )
    recent = or_(
        Fixture.date.is_(None),
        Fixture.date >= now - timedelta(hours=CONTROL_REFRESH_HOURS),
    )
    rows = (
        await session.execute(
            select(AdvisorDecision, Fixture)
            .join(Fixture, Fixture.id == AdvisorDecision.fixture_id)
            .where(AdvisorDecision.decision.in_(_COUNTED))
            .where(AdvisorDecision.n.is_not(None))
            .where(Fixture.status_short.in_(tuple(FINISHED_FIXTURE_STATUSES)))
            .where(or_(missing, recent))
        )
    ).all()
    if not rows:
        return
    refreshed = await _refreshed(session, {int(fixture.id) for _, fixture in rows})
    known = {
        row.decision_id: row
        for row in (
            await session.scalars(
                select(AdvisorCounterfactual).where(
                    AdvisorCounterfactual.decision_id.in_(
                        [decision.id for decision, _ in rows]
                    )
                )
            )
        ).all()
    }
    for decision, fixture in rows:
        if fixture.id not in refreshed:
            continue
        n = decision.n
        if n is None:
            continue
        scored = _goal_after(decision.period, n, fixture)
        if scored is None:
            continue
        current = known.get(decision.id)
        if current is None:
            session.add(
                AdvisorCounterfactual(
                    decision_id=decision.id,
                    goal_scored=scored,
                    odd=decision.odd_at_eval,
                )
            )
            continue
        if current.goal_scored == scored and current.odd == decision.odd_at_eval:
            continue
        if _inside_correction_window(fixture, now):
            current.goal_scored = scored
            current.odd = decision.odd_at_eval


def _goal_after(period: str, n: int, fixture: Fixture) -> bool | None:
    if period == Period.EXTRA_TIME.value:
        if fixture.goals_home_extratime is None or fixture.goals_away_extratime is None:
            return None
        return fixture.goals_home_extratime + fixture.goals_away_extratime > n
    if fixture.goals_home_fulltime is None or fixture.goals_away_fulltime is None:
        return None
    return fixture.goals_home_fulltime + fixture.goals_away_fulltime > n
