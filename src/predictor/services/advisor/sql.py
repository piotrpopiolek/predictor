"""Read-only inputs for one advisor pass. Writes stay in the cycle and sandbox."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, or_, select
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
    Period,
    over_line_matches,
    over_under_market,
)


class _PeriodOverCandidate(Protocol):
    fixture_id: int
    period: str
    goals_home: int
    goals_away: int
    n: int


@dataclass(frozen=True, slots=True)
class OverUnderRow:
    fixture_id: int
    label: str
    handicap: str
    odd: Decimal
    tradable: bool
    captured_at: datetime
    market: str = REGULATION_OVER_UNDER
    home_goals: int | None = None
    away_goals: int | None = None


@dataclass(frozen=True, slots=True)
class FirstOver:
    """Earliest same-score Over N+0.5 in the candidate's settlement market."""

    odd: Decimal
    minute: int | None


@dataclass(frozen=True, slots=True)
class _OverHistoryRow:
    fixture_id: int
    market: str
    label: str
    handicap: str
    odd: Decimal
    captured_at: datetime
    home_goals: int | None
    away_goals: int | None
    elapsed_minutes: int | None


_OVER_UNDER_MARKETS = frozenset(
    name for name in (REGULATION_OVER_UNDER, EXTRA_TIME_OVER_UNDER) if name is not None
)
_REGULATION_FIRST_MINUTE = 45


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
    market_by_bet = {int(bet_id): str(name).casefold() for bet_id, name in bets}
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
                FixtureOddsLive.home_goals,
                FixtureOddsLive.away_goals,
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
                home_goals=row.home_goals,
                away_goals=row.away_goals,
            )
        )
    return {fixture_id: tuple(items) for fixture_id, items in grouped.items()}


async def first_period_over(
    session: AsyncSession, candidates: Sequence[_PeriodOverCandidate]
) -> dict[int, FirstOver]:
    """First tradable Over N+0.5 at the candidate board in that period's market.

    Regulation rows need ``elapsed_minutes >= 45``. Extra-time rows have no
    minute floor. Suspended / stopped / blocked / finished snapshots drop out.
    """
    if not candidates:
        return {}
    fixture_ids = sorted({item.fixture_id for item in candidates})
    bets = (
        await session.execute(
            select(OddsLiveBet.id, OddsLiveBet.name).where(
                func.lower(OddsLiveBet.name).in_(_OVER_UNDER_MARKETS)
            )
        )
    ).all()
    if not bets:
        return {}
    market_by_bet = {int(bet_id): str(name).casefold() for bet_id, name in bets}
    bet_ids = list(market_by_bet)
    rows = (
        await session.execute(
            select(
                FixtureOddsLive.fixture_id,
                FixtureOddsLive.bet_id,
                FixtureOddsLive.value_label,
                FixtureOddsLive.handicap,
                FixtureOddsLive.odd,
                FixtureOddsLive.captured_at,
                FixtureOddsLive.home_goals,
                FixtureOddsLive.away_goals,
                FixtureOddsLive.elapsed_minutes,
            )
            .where(FixtureOddsLive.fixture_id.in_(fixture_ids))
            .where(FixtureOddsLive.bet_id.in_(bet_ids))
            .where(func.lower(FixtureOddsLive.value_label).like("over%"))
            .where(
                or_(
                    FixtureOddsLive.suspended.is_(None),
                    FixtureOddsLive.suspended.is_(False),
                )
            )
            .where(
                or_(
                    FixtureOddsLive.stopped.is_(None),
                    FixtureOddsLive.stopped.is_(False),
                )
            )
            .where(
                or_(
                    FixtureOddsLive.blocked.is_(None),
                    FixtureOddsLive.blocked.is_(False),
                )
            )
            .where(
                or_(
                    FixtureOddsLive.finished.is_(None),
                    FixtureOddsLive.finished.is_(False),
                )
            )
        )
    ).all()
    history: dict[int, list[_OverHistoryRow]] = {}
    for row in rows:
        market = market_by_bet.get(int(row.bet_id))
        if market is None:
            continue
        elapsed = row.elapsed_minutes
        if market == REGULATION_OVER_UNDER and (
            elapsed is None or int(elapsed) < _REGULATION_FIRST_MINUTE
        ):
            continue
        fixture_id = int(row.fixture_id)
        history.setdefault(fixture_id, []).append(
            _OverHistoryRow(
                fixture_id=fixture_id,
                market=market,
                label=str(row.value_label),
                handicap=str(row.handicap or ""),
                odd=row.odd,
                captured_at=row.captured_at,
                home_goals=row.home_goals,
                away_goals=row.away_goals,
                elapsed_minutes=elapsed if elapsed is None else int(elapsed),
            )
        )
    out: dict[int, FirstOver] = {}
    for candidate in candidates:
        first = _first_over_for(candidate, history.get(candidate.fixture_id, ()))
        if first is not None:
            out[candidate.fixture_id] = first
    return out


def _first_over_for(
    candidate: _PeriodOverCandidate, rows: Sequence[_OverHistoryRow]
) -> FirstOver | None:
    try:
        period = Period(candidate.period)
    except ValueError:
        return None
    market = over_under_market(period)
    if market is None:
        return None
    board = (candidate.goals_home, candidate.goals_away)
    matched = [
        row
        for row in rows
        if row.market == market
        and (row.home_goals, row.away_goals) == board
        and over_line_matches(row.label, row.handicap, candidate.n)
    ]
    if not matched:
        return None
    earliest = min(matched, key=lambda row: row.captured_at)
    return FirstOver(odd=earliest.odd, minute=earliest.elapsed_minutes)


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
                    _candidate_ordinal(item),
                )
            )
        except KeyError, TypeError, ValueError:
            continue
    return tuple(sorted(keys))


def _candidate_ordinal(item: dict[str, object]) -> int:
    """Ordinal from body, or ``n + 1`` for p5 payloads that omit it."""
    if "ordinal" in item:
        return int(item["ordinal"])  # type: ignore[arg-type]
    return int(item["n"]) + 1  # type: ignore[arg-type]
