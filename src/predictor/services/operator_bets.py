"""Operator next-goal bet ledger. Written only by the status process."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from predictor.models.children import FixtureEvent
from predictor.models.fixtures import Fixture
from predictor.models.operator_bets import OperatorBet
from predictor.services.live_board import LiveMatch

START_STAKE = Decimal("21.00")
STAKE_STEP = Decimal("0.10")
PAYOUT_KEEP = Decimal("0.880")
ODD_MIN = Decimal("1.200")
ODD_MAX = Decimal("6.000")

BetStatus = Literal["open", "won", "lost", "void"]
SettleOutcome = Literal["won", "lost", "void"]

_FINISHED_NO_GOAL = frozenset({"FT", "AET", "PEN"})
_LEAVE_OPEN = frozenset({"PST", "CANC", "ABD", "AWD", "WO"})
_SETTLE_OUTCOMES = frozenset({"won", "lost", "void"})
_GOAL_EVENT = "Goal"
_MISSED_PENALTY = "missed penalty"


class BetError(Exception):
    """Domain error for place/settle. ``code`` maps to HTTP status."""

    def __init__(self, code: int, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def odd_value(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def parse_odd(raw: str | float | Decimal) -> Decimal:
    try:
        odd = Decimal(str(raw).strip().replace(",", "."))
    except Exception as exc:
        raise BetError(400, "invalid_odd") from exc
    if odd < ODD_MIN or odd > ODD_MAX:
        raise BetError(400, "odd_out_of_range")
    return odd_value(odd)


def row_pnl(bet: OperatorBet) -> Decimal:
    keep = Decimal(bet.payout_keep)
    stake = Decimal(bet.stake)
    odd = Decimal(bet.odd)
    if bet.status == "won":
        return money(stake * odd * keep - stake)
    if bet.status == "lost":
        return money(-stake)
    return Decimal("0.00")


async def count_wins(session: AsyncSession) -> int:
    result = await session.scalar(
        select(func.count()).select_from(OperatorBet).where(OperatorBet.status == "won")
    )
    return int(result or 0)


async def current_stake(session: AsyncSession) -> Decimal:
    wins = await count_wins(session)
    return money(START_STAKE + STAKE_STEP * wins)


def goal_counts_for_next_goal(minute: int | None, minute_extra: int | None) -> bool:
    """Any timed Goal event counts, including stoppage (90+x)."""
    del minute_extra
    return minute is not None


def _scoring_side(
    *,
    event_team_id: int | None,
    home_id: int,
    away_id: int,
    detail: str | None,
) -> Literal["home", "away"] | None:
    if event_team_id is None:
        return None
    det = (detail or "").strip().casefold()
    if det == _MISSED_PENALTY:
        return None
    team_id = int(event_team_id)
    if det == "own goal":
        if team_id == home_id:
            return "away"
        if team_id == away_id:
            return "home"
        return None
    if team_id == home_id:
        return "home"
    if team_id == away_id:
        return "away"
    return None


def has_counting_goal_since_snapshot(
    events: Sequence[Any],
    *,
    home_id: int,
    away_id: int,
    snap_home: int,
    snap_away: int,
) -> bool:
    """True if any goal advances the score past the bet snapshot."""
    home = 0
    away = 0
    for event in events:
        if str(getattr(event, "event_type", "") or "") != _GOAL_EVENT:
            continue
        side = _scoring_side(
            event_team_id=getattr(event, "team_id", None),
            home_id=home_id,
            away_id=away_id,
            detail=getattr(event, "detail", None),
        )
        if side is None:
            continue
        if side == "home":
            home += 1
        else:
            away += 1
        if home <= snap_home and away <= snap_away:
            continue
        if not goal_counts_for_next_goal(
            getattr(event, "minute", None),
            getattr(event, "minute_extra", None),
        ):
            continue
        if home > snap_home or away > snap_away:
            return True
    return False


def resolve_auto_outcome(
    *,
    snap_home: int,
    snap_away: int,
    now_home: int,
    now_away: int,
    status_short: str | None,
    counting_goal: bool = False,
    elapsed: int | None = None,
    has_goal_events: bool = False,
) -> SettleOutcome | None:
    """Settle 'padnie gol': any next goal → won; FT without → lost."""
    del elapsed
    if counting_goal:
        return "won"

    home_delta = now_home - snap_home
    away_delta = now_away - snap_away
    if home_delta < 0 or away_delta < 0:
        return None

    short = (status_short or "").upper()
    if short in _LEAVE_OPEN:
        return None

    score_changed = home_delta > 0 or away_delta > 0
    if short in _FINISHED_NO_GOAL:
        if has_goal_events or not score_changed:
            return "lost"
        # Finished with score change, no event rows — treat as goal scored.
        return "won"

    if not score_changed:
        return None
    if has_goal_events:
        # Events loaded but none past snapshot yet.
        return None
    # Live score moved without event timing → goal scored.
    return "won"


async def settle_open_tickets(session: AsyncSession) -> int:
    """Settle open tickets from fixture score/status/events. Returns count settled."""
    open_rows = (
        await session.scalars(
            select(OperatorBet).where(OperatorBet.status == "open").order_by(OperatorBet.id)
        )
    ).all()
    if not open_rows:
        return 0
    fixture_ids = {row.fixture_id for row in open_rows}
    fixtures = {
        int(row.id): row
        for row in (
            await session.scalars(select(Fixture).where(Fixture.id.in_(fixture_ids)))
        ).all()
    }
    events_by_fixture: dict[int, list[FixtureEvent]] = {fid: [] for fid in fixture_ids}
    if fixture_ids:
        event_rows = (
            await session.scalars(
                select(FixtureEvent)
                .where(FixtureEvent.fixture_id.in_(fixture_ids))
                .where(FixtureEvent.event_type == _GOAL_EVENT)
                .order_by(
                    FixtureEvent.fixture_id,
                    FixtureEvent.minute.asc().nulls_last(),
                    FixtureEvent.minute_extra.asc().nulls_last(),
                    FixtureEvent.id.asc(),
                )
            )
        ).all()
        for event in event_rows:
            events_by_fixture.setdefault(int(event.fixture_id), []).append(event)

    now = datetime.now(UTC)
    settled = 0
    for bet in open_rows:
        fixture = fixtures.get(bet.fixture_id)
        if fixture is None:
            continue
        now_home = int(fixture.goals_home or 0)
        now_away = int(fixture.goals_away or 0)
        events = events_by_fixture.get(bet.fixture_id, [])
        counting_goal = has_counting_goal_since_snapshot(
            events,
            home_id=int(fixture.home_team_id),
            away_id=int(fixture.away_team_id),
            snap_home=int(bet.goals_home),
            snap_away=int(bet.goals_away),
        )
        outcome = resolve_auto_outcome(
            snap_home=int(bet.goals_home),
            snap_away=int(bet.goals_away),
            now_home=now_home,
            now_away=now_away,
            status_short=fixture.status_short,
            counting_goal=counting_goal,
            elapsed=fixture.elapsed_minutes,
            has_goal_events=bool(events),
        )
        if outcome is None:
            continue
        bet.status = outcome
        bet.settled_at = now
        settled += 1
    return settled


async def place_bet(
    session: AsyncSession,
    *,
    match: LiveMatch,
    odd: Decimal,
) -> OperatorBet:
    existing = await session.scalar(
        select(OperatorBet.id).where(
            OperatorBet.fixture_id == match.fixture_id,
            OperatorBet.status == "open",
        )
    )
    if existing is not None:
        raise BetError(409, "open_bet_exists")
    stake = await current_stake(session)
    bet = OperatorBet(
        fixture_id=match.fixture_id,
        odd=odd,
        stake=stake,
        status="open",
        league=f"{match.country} · {match.league}".strip(" ·"),
        home_name=match.home,
        away_name=match.away,
        goals_home=int(match.goals_home or 0),
        goals_away=int(match.goals_away or 0),
        elapsed=match.elapsed,
        status_short=match.status_short,
        payout_keep=PAYOUT_KEEP,
    )
    session.add(bet)
    await session.flush()
    return bet


async def settle_bet_manual(
    session: AsyncSession,
    bet_id: int,
    outcome: str,
) -> OperatorBet:
    value = outcome.strip().lower()
    if value not in _SETTLE_OUTCOMES:
        raise BetError(400, "invalid_outcome")
    bet = await session.get(OperatorBet, bet_id)
    if bet is None:
        raise BetError(404, "bet_not_found")
    if bet.status != "open":
        raise BetError(409, "already_settled")
    bet.status = value
    bet.settled_at = datetime.now(UTC)
    await session.flush()
    return bet


@dataclass(frozen=True, slots=True)
class BetHistoryRow:
    id: int
    fixture_id: int
    placed_at: datetime
    settled_at: datetime | None
    league: str
    home: str
    away: str
    odd: Decimal
    stake: Decimal
    status: str
    pnl: Decimal
    saldo: Decimal
    hit_overall: float | None
    hit_last10: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "fixture_id": self.fixture_id,
            "placed_at": self.placed_at.isoformat(),
            "settled_at": None if self.settled_at is None else self.settled_at.isoformat(),
            "league": self.league,
            "home": self.home,
            "away": self.away,
            "odd": format(self.odd, "f"),
            "stake": format(self.stake, "f"),
            "status": self.status,
            "pnl": format(self.pnl, "f"),
            "saldo": format(self.saldo, "f"),
            "hit_overall": self.hit_overall,
            "hit_last10": self.hit_last10,
        }


@dataclass(frozen=True, slots=True)
class BetMetrics:
    n: int
    wins: int
    losses: int
    voids: int
    open_count: int
    hit_rate: float | None
    saldo: Decimal
    current_stake: Decimal
    avg_odd: Decimal | None
    staked: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "wins": self.wins,
            "losses": self.losses,
            "voids": self.voids,
            "open_count": self.open_count,
            "hit_rate": self.hit_rate,
            "saldo": format(self.saldo, "f"),
            "current_stake": format(self.current_stake, "f"),
            "avg_odd": None if self.avg_odd is None else format(self.avg_odd, "f"),
            "staked": format(self.staked, "f"),
        }


def _hit_rate(wins: int, losses: int) -> float | None:
    decided = wins + losses
    if decided == 0:
        return None
    return round(100.0 * wins / decided, 2)


def build_history(bets: list[OperatorBet]) -> tuple[list[BetHistoryRow], BetMetrics]:
    ordered = sorted(bets, key=lambda row: (row.placed_at, row.id))
    saldo = Decimal("0.00")
    wins = losses = voids = open_count = 0
    decided_results: list[bool] = []
    rows: list[BetHistoryRow] = []
    odd_sum = Decimal("0")
    staked = Decimal("0.00")

    for bet in ordered:
        pnl = row_pnl(bet)
        if bet.status in {"won", "lost", "void"}:
            saldo = money(saldo + pnl)
        staked = money(staked + Decimal(bet.stake))
        odd_sum += Decimal(bet.odd)
        if bet.status == "won":
            wins += 1
            decided_results.append(True)
        elif bet.status == "lost":
            losses += 1
            decided_results.append(False)
        elif bet.status == "void":
            voids += 1
        else:
            open_count += 1

        hit_overall = _hit_rate(wins, losses)
        last10 = decided_results[-10:]
        hit_last10 = None
        if last10:
            hit_last10 = round(100.0 * sum(1 for x in last10 if x) / len(last10), 2)

        rows.append(
            BetHistoryRow(
                id=int(bet.id),
                fixture_id=int(bet.fixture_id),
                placed_at=bet.placed_at,
                settled_at=bet.settled_at,
                league=bet.league,
                home=bet.home_name,
                away=bet.away_name,
                odd=Decimal(bet.odd),
                stake=Decimal(bet.stake),
                status=bet.status,
                pnl=pnl,
                saldo=saldo,
                hit_overall=hit_overall,
                hit_last10=hit_last10,
            )
        )

    n = len(ordered)
    metrics = BetMetrics(
        n=n,
        wins=wins,
        losses=losses,
        voids=voids,
        open_count=open_count,
        hit_rate=_hit_rate(wins, losses),
        saldo=saldo,
        current_stake=money(START_STAKE + STAKE_STEP * wins),
        avg_odd=None if n == 0 else odd_value(odd_sum / n),
        staked=staked,
    )
    return rows, metrics


async def list_bets(session: AsyncSession) -> list[OperatorBet]:
    result = await session.scalars(
        select(OperatorBet).order_by(OperatorBet.placed_at.asc(), OperatorBet.id.asc())
    )
    return list(result.all())


async def list_open_by_fixture(session: AsyncSession) -> dict[int, OperatorBet]:
    rows = (
        await session.scalars(select(OperatorBet).where(OperatorBet.status == "open"))
    ).all()
    return {int(row.fixture_id): row for row in rows}


async def load_history_payload(engine: AsyncEngine) -> dict[str, Any]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await settle_open_tickets(session)
        await session.commit()
        bets = await list_bets(session)
        rows, metrics = build_history(bets)
        stake = await current_stake(session)
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "metrics": {**metrics.as_dict(), "current_stake": format(stake, "f")},
            "bets": [row.as_dict() for row in rows],
        }


async def settle_open_from_engine(engine: AsyncEngine) -> int:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        settled = await settle_open_tickets(session)
        await session.commit()
        return settled


async def current_stake_label(engine: AsyncEngine) -> str:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        stake = await current_stake(session)
        return format(stake, "f")


async def settle_and_list_open(
    engine: AsyncEngine,
) -> dict[int, OperatorBet]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await settle_open_tickets(session)
        await session.commit()
        return await list_open_by_fixture(session)


def bet_as_dict(bet: OperatorBet) -> dict[str, Any]:
    return {
        "id": int(bet.id),
        "fixture_id": int(bet.fixture_id),
        "odd": format(Decimal(bet.odd), "f"),
        "stake": format(Decimal(bet.stake), "f"),
        "status": bet.status,
        "placed_at": bet.placed_at.isoformat(),
        "settled_at": None if bet.settled_at is None else bet.settled_at.isoformat(),
        "league": bet.league,
        "home": bet.home_name,
        "away": bet.away_name,
        "goals_home": int(bet.goals_home),
        "goals_away": int(bet.goals_away),
        "pnl": format(row_pnl(bet), "f"),
    }
