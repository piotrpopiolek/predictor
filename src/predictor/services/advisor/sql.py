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
from predictor.services.advisor.domain import REGULATION_OVER_UNDER


@dataclass(frozen=True, slots=True)
class OverUnderRow:
    fixture_id: int
    label: str
    handicap: str
    odd: Decimal
    tradable: bool
    captured_at: datetime


async def latest_over_under(
    session: AsyncSession, fixture_ids: Sequence[int]
) -> dict[int, tuple[OverUnderRow, ...]]:
    """Rows from the newest Over/Under Line snapshot of each fixture."""
    if not fixture_ids:
        return {}
    bet_ids = list(
        await session.scalars(
            select(OddsLiveBet.id).where(
                func.lower(OddsLiveBet.name) == REGULATION_OVER_UNDER
            )
        )
    )
    if not bet_ids:
        return {}
    latest = (
        select(
            FixtureOddsLive.fixture_id,
            func.max(FixtureOddsLive.captured_at).label("captured_at"),
        )
        .where(FixtureOddsLive.fixture_id.in_(fixture_ids))
        .where(FixtureOddsLive.bet_id.in_(bet_ids))
        .group_by(FixtureOddsLive.fixture_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                FixtureOddsLive.fixture_id,
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
                & (FixtureOddsLive.captured_at == latest.c.captured_at),
            )
            .where(FixtureOddsLive.bet_id.in_(bet_ids))
        )
    ).all()
    grouped: dict[int, list[OverUnderRow]] = {}
    for row in rows:
        fixture_id = int(row.fixture_id)
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


async def llm_open_coupon_fixtures(
    session: AsyncSession, fixture_ids: Sequence[int] | None = None
) -> frozenset[int]:
    """Fixtures on an open sandbox leg of any LLM strategy."""
    stmt = (
        select(AdvisorLeg.fixture_id)
        .join(AdvisorStrategy, AdvisorStrategy.id == AdvisorLeg.strategy_id)
        .where(AdvisorStrategy.kind == "llm")
        .where(AdvisorLeg.mode == "sandbox")
        .where(AdvisorLeg.status.in_(("open", "manual")))
    )
    if fixture_ids is not None:
        if not fixture_ids:
            return frozenset()
        stmt = stmt.where(AdvisorLeg.fixture_id.in_(fixture_ids))
    rows = await session.scalars(stmt)
    return frozenset(int(fixture_id) for fixture_id in rows)


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
