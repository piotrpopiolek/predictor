"""Sandbox portfolios.

The baseline bets every playable candidate as one Over N+0.5 leg. An LLM
strategy bets only coupons from a validated reply. Both use the shared
stake rule and the same virtual bankroll. This module does not call a model.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.constants import PAYOUT_KEEP, SANDBOX_BANKROLL, stake_after_wins
from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorLeg,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.services.advisor.domain import Settlement, combined_odd, settled_pnl

_MONEY = Decimal("0.01")
_ACTIVE = frozenset({"sandbox", "promoted"})
_OPEN_LEG = "uq_advisor_legs_open_sandbox"


class CouponLeg(Protocol):
    """Read-only. A frozen candidate has no setters, so neither does this."""

    @property
    def fixture_id(self) -> int: ...

    @property
    def period(self) -> str: ...

    @property
    def goals_home(self) -> int: ...

    @property
    def goals_away(self) -> int: ...

    @property
    def odd(self) -> Decimal: ...

    @property
    def n(self) -> int: ...


@dataclass(frozen=True, slots=True)
class LegSnapshot:
    fixture_id: int
    period: str
    goals_home: int
    goals_away: int
    odd: Decimal
    n: int


@dataclass(frozen=True, slots=True)
class SandboxSlip:
    """One coupon. Baseline slips have a single leg; the model may combine."""

    legs: tuple[LegSnapshot, ...]


@dataclass(frozen=True, slots=True)
class SandboxPlacement:
    coupon_ids: tuple[int, ...]
    paused: bool


def baseline_slips(candidates: Sequence[CouponLeg]) -> tuple[SandboxSlip, ...]:
    """One single-leg slip per candidate. No odd floor and no model.

    A repeated fixture is kept once, the first time it appears. An odd at
    or below 1 cannot be stored, so that candidate is left out.
    """
    seen: set[int] = set()
    slips: list[SandboxSlip] = []
    for candidate in candidates:
        if candidate.fixture_id in seen or candidate.odd <= 1:
            continue
        seen.add(candidate.fixture_id)
        slips.append(SandboxSlip((_snapshot(candidate),)))
    return tuple(slips)


def reply_slips(
    coupons: Sequence[Sequence[int]],
    candidates: Sequence[CouponLeg],
    stored: Collection[int],
) -> tuple[SandboxSlip, ...]:
    """Coupons whose every leg was actually stored. A partial coupon is dropped."""
    by_id = {item.fixture_id: _snapshot(item) for item in candidates}
    slips: list[SandboxSlip] = []
    for fixtures in coupons:
        missing = any(
            fixture_id not in stored or fixture_id not in by_id
            for fixture_id in fixtures
        )
        if not fixtures or missing:
            continue
        slips.append(SandboxSlip(tuple(by_id[fixture_id] for fixture_id in fixtures)))
    return tuple(slips)


def portfolio_balance(
    rows: Sequence[tuple[Decimal, Decimal | None, Decimal, str]],
) -> Decimal:
    """Bankroll after open stakes and settled coupons.

    An open coupon has taken its stake. A win adds the net payout, a loss
    keeps the stake spent, and a void returns it. Other statuses never
    took money.
    """
    balance = SANDBOX_BANKROLL
    for stake, odd, keep, status in rows:
        if status == "open":
            balance -= stake
        elif status == "won":
            if odd is None:
                raise ValueError("won coupon has no odd")
            balance += settled_pnl(stake, odd, Settlement.WON, keep)
        elif status == "lost":
            balance -= stake
        elif status == "void":
            continue
    return balance.quantize(_MONEY, rounding=ROUND_HALF_UP)


def _clock() -> datetime:
    return datetime.now(UTC)


async def release_funded_strategies(session: AsyncSession) -> None:
    """Return a paused strategy to the sandbox once the book covers the stake."""
    if not session.in_transaction():
        raise RuntimeError("release_funded_strategies must run inside a transaction")
    rows = (
        await session.scalars(
            select(AdvisorStrategy).where(AdvisorStrategy.status == "paused")
        )
    ).all()
    for strategy in rows:
        wins = await _won_count(session, strategy.id)
        balance = portfolio_balance(await _coupon_rows(session, strategy.id))
        if balance >= stake_after_wins(wins):
            strategy.status = "sandbox"


async def place_baseline(
    session: AsyncSession,
    *,
    strategy_id: int,
    strategy_version_id: int,
    candidates: Sequence[CouponLeg],
    clock: Callable[[], datetime] = _clock,
) -> SandboxPlacement:
    """Bet each playable candidate and commit. Does not call a model.

    The strategy must be the baseline kind. A paused strategy, or one that
    cannot cover the stake, places nothing further; the latter is paused.
    """
    if session.in_transaction():
        raise RuntimeError("place_baseline must be called without an open transaction")
    slips = baseline_slips(candidates)
    if not slips:
        return SandboxPlacement((), False)
    async with session.begin():
        return await place_sandbox_coupons(
            session,
            strategy_id=strategy_id,
            strategy_version_id=strategy_version_id,
            slips=slips,
            cycle_id=None,
            placed_at=clock(),
            kind="baseline",
        )


async def place_sandbox_coupons(
    session: AsyncSession,
    *,
    strategy_id: int,
    strategy_version_id: int,
    slips: Sequence[SandboxSlip],
    cycle_id: int | None,
    placed_at: datetime,
    kind: str,
) -> SandboxPlacement:
    """Insert slips inside the caller's transaction. Locks the strategy row."""
    if not session.in_transaction():
        raise RuntimeError("place_sandbox_coupons must run inside a transaction")
    if placed_at.tzinfo is None:
        raise ValueError("placed_at must be timezone-aware")
    strategy = await session.scalar(
        select(AdvisorStrategy)
        .where(AdvisorStrategy.id == strategy_id)
        .with_for_update()
    )
    if strategy is None:
        raise ValueError("unknown strategy")
    version = await session.get(AdvisorStrategyVersion, strategy_version_id)
    if version is None or version.strategy_id != strategy.id:
        raise ValueError("strategy version does not belong to the strategy")
    if strategy.kind != kind:
        raise ValueError("strategy kind does not match")
    if strategy.status not in _ACTIVE:
        return SandboxPlacement((), strategy.status == "paused")
    if not slips:
        return SandboxPlacement((), False)
    balance = portfolio_balance(await _coupon_rows(session, strategy.id))
    stake = stake_after_wins(await _won_count(session, strategy.id))
    placed: list[int] = []
    for slip in slips:
        if balance < stake:
            break
        coupon_id = await _insert_slip(
            session,
            slip,
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            cycle_id=cycle_id,
            placed_at=placed_at,
            stake=stake,
        )
        if coupon_id is None:
            continue
        placed.append(coupon_id)
        balance -= stake
    if balance < stake:
        strategy.status = "paused"
    return SandboxPlacement(tuple(placed), strategy.status == "paused")


def _snapshot(leg: CouponLeg) -> LegSnapshot:
    return LegSnapshot(
        fixture_id=leg.fixture_id,
        period=leg.period,
        goals_home=leg.goals_home,
        goals_away=leg.goals_away,
        odd=leg.odd,
        n=leg.n,
    )


async def _won_count(session: AsyncSession, strategy_id: int) -> int:
    """Settled wins of this strategy. Open and lost coupons do not raise the stake."""
    result = await session.scalar(
        select(func.count())
        .select_from(AdvisorCoupon)
        .join(
            AdvisorStrategyVersion,
            AdvisorCoupon.strategy_version_id == AdvisorStrategyVersion.id,
        )
        .where(AdvisorStrategyVersion.strategy_id == strategy_id)
        .where(AdvisorCoupon.mode == "sandbox")
        .where(AdvisorCoupon.status == "won")
    )
    return int(result or 0)


async def _coupon_rows(
    session: AsyncSession, strategy_id: int
) -> tuple[tuple[Decimal, Decimal | None, Decimal, str], ...]:
    result = await session.execute(
        select(
            AdvisorCoupon.stake,
            AdvisorCoupon.combined_odd,
            AdvisorCoupon.payout_keep,
            AdvisorCoupon.status,
        )
        .join(
            AdvisorStrategyVersion,
            AdvisorCoupon.strategy_version_id == AdvisorStrategyVersion.id,
        )
        .where(
            AdvisorStrategyVersion.strategy_id == strategy_id,
            AdvisorCoupon.mode == "sandbox",
        )
    )
    return tuple(
        (stake, odd, keep, status) for stake, odd, keep, status in result.all()
    )


async def _insert_slip(
    session: AsyncSession,
    slip: SandboxSlip,
    *,
    strategy_id: int,
    strategy_version_id: int,
    cycle_id: int | None,
    placed_at: datetime,
    stake: Decimal,
) -> int | None:
    """None when this match is already on an open coupon of the strategy."""
    odd = combined_odd(tuple(leg.odd for leg in slip.legs))
    try:
        async with session.begin_nested():
            coupon = AdvisorCoupon(
                mode="sandbox",
                strategy_version_id=strategy_version_id,
                cycle_id=cycle_id,
                status="open",
                stake=stake,
                combined_odd=odd,
                proposed_combined_odd=odd,
                payout_keep=PAYOUT_KEEP,
                placed_at=placed_at,
            )
            session.add(coupon)
            await session.flush()
            for leg in slip.legs:
                session.add(
                    AdvisorLeg(
                        coupon_id=coupon.id,
                        strategy_id=strategy_id,
                        fixture_id=leg.fixture_id,
                        mode="sandbox",
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
            return coupon.id
    except IntegrityError as exc:
        if _is_open_leg_conflict(exc):
            return None
        raise


def _is_open_leg_conflict(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    return getattr(diag, "constraint_name", None) == _OPEN_LEG
