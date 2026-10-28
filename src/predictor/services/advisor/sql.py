"""Read-only inputs for one advisor pass. Writes stay in the cycle and sandbox."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.models.advisor import (
    AdvisorCycle,
    AdvisorDecision,
    AdvisorLeg,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.models.catalog import OddsLiveBet
from predictor.models.odds import FixtureOddsLive
from predictor.services.advisor.domain import (
    EXTRA_TIME_OVER_UNDER,
    REGULATION_OVER_UNDER,
)


@dataclass(frozen=True, slots=True)
class OverUnderRow:
    fixture_id: int
    label: str
    handicap: str
    odd: Decimal
    tradable: bool
    captured_at: datetime
    market: str = REGULATION_OVER_UNDER


_OVER_UNDER_MARKETS = frozenset(
    name
    for name in (REGULATION_OVER_UNDER, EXTRA_TIME_OVER_UNDER)
    if name is not None
)


async def latest_over_under(
    session: AsyncSession, fixture_ids: Sequence[int]
) -> dict[int, tuple[OverUnderRow, ...]]:
    """Newest regulation and extra-time Over/Under snapshots per fixture."""
    if not fixture_ids:
        return {}
    bets = (
        await session.execute(
            select(OddsLiveBet.id, OddsLiveBet.name).where(
                func.lower(OddsLiveBet.name).in_(_OVER_UNDER_MARKETS)
            )
        )
    ).all()
    if not bets:
        return {}
    market_by_bet = {
        int(bet_id): str(name).casefold() for bet_id, name in bets
    }
    bet_ids = list(market_by_bet)
    latest = (
        select(
            FixtureOddsLive.fixture_id,
            FixtureOddsLive.bet_id,
            func.max(FixtureOddsLive.captured_at).label("captured_at"),
        )
        .where(FixtureOddsLive.fixture_id.in_(fixture_ids))
        .where(FixtureOddsLive.bet_id.in_(bet_ids))
        .group_by(FixtureOddsLive.fixture_id, FixtureOddsLive.bet_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                FixtureOddsLive.fixture_id,
                FixtureOddsLive.bet_id,
                FixtureOddsLive.value_label,
                FixtureOddsLive.handicap,
                FixtureOddsLive.odd,
                FixtureOddsLive.suspended,
                FixtureOddsLive.stopped,
                FixtureOddsLive.blocked,
                FixtureOddsLive.finished,
                FixtureOddsLive.captured_at,
            )
            .join(
                latest,
                (FixtureOddsLive.fixture_id == latest.c.fixture_id)
                & (FixtureOddsLive.bet_id == latest.c.bet_id)
                & (FixtureOddsLive.captured_at == latest.c.captured_at),
            )
            .where(FixtureOddsLive.bet_id.in_(bet_ids))
        )
    ).all()
    grouped: dict[int, list[OverUnderRow]] = {}
    for row in rows:
        fixture_id = int(row.fixture_id)
        market = market_by_bet.get(int(row.bet_id))
        if market is None:
            continue
        grouped.setdefault(fixture_id, []).append(
            OverUnderRow(
                fixture_id=fixture_id,
                label=str(row.value_label),
                handicap=str(row.handicap or ""),
                odd=row.odd,
                tradable=not any(
                    flag is True
                    for flag in (row.suspended, row.stopped, row.blocked, row.finished)
                ),
                captured_at=row.captured_at,
                market=market,
            )
        )
    return {fixture_id: tuple(items) for fixture_id, items in grouped.items()}


async def open_fixture_ids(session: AsyncSession, strategy_id: int) -> frozenset[int]:
    rows = await session.scalars(
        select(AdvisorLeg.fixture_id).where(
            AdvisorLeg.strategy_id == strategy_id,
            AdvisorLeg.mode == "sandbox",
            AdvisorLeg.status.in_(("open", "manual")),
        )
    )
    return frozenset(int(fixture_id) for fixture_id in rows)


async def open_sandbox_coupon_labels(
    session: AsyncSession, fixture_ids: Sequence[int] | None = None
) -> dict[int, str]:
    """Corner badge text for fixtures on an open sandbox leg.

    LLM wins the label when both baseline and LLM cover the same match.
    """
    stmt = (
        select(AdvisorLeg.fixture_id, AdvisorStrategy.kind)
        .join(AdvisorStrategy, AdvisorStrategy.id == AdvisorLeg.strategy_id)
        .where(AdvisorStrategy.kind.in_(("baseline", "llm")))
        .where(AdvisorLeg.mode == "sandbox")
        .where(AdvisorLeg.status.in_(("open", "manual")))
    )
    if fixture_ids is not None:
        if not fixture_ids:
            return {}
        stmt = stmt.where(AdvisorLeg.fixture_id.in_(fixture_ids))
    labels: dict[int, str] = {}
    for fixture_id, kind in (await session.execute(stmt)).all():
        fid = int(fixture_id)
        if kind == "llm":
            labels[fid] = "Kupon LLM"
        elif fid not in labels:
            labels[fid] = "Kupon"
    return labels


async def llm_open_coupon_fixtures(
    session: AsyncSession, fixture_ids: Sequence[int] | None = None
) -> frozenset[int]:
    """Fixtures on an open sandbox leg of any LLM strategy."""
    labels = await open_sandbox_coupon_labels(session, fixture_ids)
    return frozenset(
        fixture_id for fixture_id, label in labels.items() if label == "Kupon LLM"
    )


async def rejected_states(
    session: AsyncSession, strategy_id: int
) -> frozenset[tuple[int, str, int, int, int]]:
    rows = (
        await session.execute(
            select(
                AdvisorDecision.fixture_id,
                AdvisorDecision.period,
                AdvisorDecision.goals_home,
                AdvisorDecision.goals_away,
                AdvisorDecision.ordinal,
            ).where(
                AdvisorDecision.strategy_id == strategy_id,
                AdvisorDecision.decision.in_(("reject", "wait_timeout")),
            )
        )
    ).all()
    return frozenset(
        (
            int(row.fixture_id),
            str(row.period),
            row.goals_home,
            row.goals_away,
            row.ordinal,
        )
        for row in rows
    )


async def last_signature(
    session: AsyncSession, strategy_id: int
) -> tuple[tuple[int, str, int, int, int], ...]:
    """State keys stored on the newest cycle. Empty when this strategy never called."""
    raw = await session.scalar(
        select(AdvisorCycle.input_json)
        .join(
            AdvisorStrategyVersion,
            AdvisorStrategyVersion.id == AdvisorCycle.strategy_version_id,
        )
        .where(AdvisorStrategyVersion.strategy_id == strategy_id)
        .order_by(AdvisorCycle.id.desc())
        .limit(1)
    )
    if not isinstance(raw, dict):
        return ()
    candidates = raw.get("candidates")
    if not isinstance(candidates, list):
        return ()
    keys: list[tuple[int, str, int, int, int]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        try:
            keys.append(
                (
                    int(item["fixture_id"]),
                    str(item["period"]),
                    int(item["goals_home"]),
                    int(item["goals_away"]),
                    int(item["ordinal"]),
                )
            )
        except KeyError, TypeError, ValueError:
            continue
    return tuple(sorted(keys))
