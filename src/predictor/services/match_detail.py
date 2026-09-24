"""Read-only match page: events, stats, lineups, prediction, odds, H2H."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from html import escape
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import aliased

from predictor.constants import FINISHED_FIXTURE_STATUSES, IN_PLAY_FIXTURE_STATUSES
from predictor.models.catalog import OddsLiveBet, Player
from predictor.models.children import (
    FixtureEvent,
    FixtureLineupPlayer,
    FixtureStatistic,
)
from predictor.models.fixtures import Fixture, Team
from predictor.models.odds import FixtureOddsLive
from predictor.models.predictions import Prediction, PredictionH2H
from predictor.models.seasonal import Standing
from predictor.services.live_board import (
    FORM_LAST_MATCHES,
    REFRESH_SECONDS,
    LiveMatch,
    _img,
    _live_nav,
    _score,
    load_fixture_match,
)

_STAT_ORDER = (
    "Ball Possession",
    "Shots on Goal",
    "Total Shots",
    "Shots off Goal",
    "Blocked Shots",
    "Shots insidebox",
    "Shots outsidebox",
    "Corner Kicks",
    "Offsides",
    "Fouls",
    "Yellow Cards",
    "Red Cards",
    "Goalkeeper Saves",
    "Total passes",
    "Passes accurate",
    "Passes %",
    "expected_goals",
)
_STAT_LABELS = {
    "Ball Possession": "Posiadanie",
    "Shots on Goal": "Strzały celne",
    "Total Shots": "Strzały",
    "Shots off Goal": "Strzały niecelne",
    "Blocked Shots": "Zablokowane",
    "Shots insidebox": "W polu karnym",
    "Shots outsidebox": "Spoza pola",
    "Corner Kicks": "Rzuty rożne",
    "Offsides": "Spalone",
    "Fouls": "Faule",
    "Yellow Cards": "Żółte kartki",
    "Red Cards": "Czerwone kartki",
    "Goalkeeper Saves": "Obrony",
    "Total passes": "Podania",
    "Passes accurate": "Podania celne",
    "Passes %": "Celność podań",
    "expected_goals": "xG",
}
_COMPARE_LABELS = {
    "form": "Forma",
    "att": "Atak",
    "def": "Obrona",
    "poisson_distribution": "Poisson",
    "h2h": "H2H",
    "goals": "Gole",
    "total": "Ogółem",
}
_EVENT_LABELS = {
    "Goal": "Gol",
    "Card": "Kartka",
    "subst": "Zmiana",
    "Var": "VAR",
}


@dataclass(frozen=True, slots=True)
class AttackChange:
    clock: str
    team: str
    summary: str
    effect: str | None


@dataclass(frozen=True, slots=True)
class GoalPrice:
    none_odd: str | None
    none_implied: str | None
    line: str
    over_odd: str | None
    over_implied: str | None
    move: str | None


ATTACK_SUB_WINDOW = 15


@dataclass(frozen=True, slots=True)
class DetailEvent:
    minute: int
    extra: int | None
    side: str
    event_type: str
    detail: str | None
    player: str | None
    assist: str | None

    @property
    def clock(self) -> str:
        if self.extra:
            return f"{self.minute}+{self.extra}'"
        return f"{self.minute}'"


@dataclass(frozen=True, slots=True)
class DetailStat:
    label: str
    home: str | None
    away: str | None


@dataclass(frozen=True, slots=True)
class DetailPlayer:
    name: str
    number: int | None
    position: str | None
    starter: bool


@dataclass(frozen=True, slots=True)
class DetailH2H:
    when: str
    home: str
    away: str
    score: str


@dataclass(frozen=True, slots=True)
class StandingSlot:
    team_id: int
    name: str
    points: int
    played: int
    gf: int
    ga: int
    api_rank: int

    @property
    def gd(self) -> int:
        return self.gf - self.ga


@dataclass(frozen=True, slots=True)
class RankedSlot:
    slot: StandingSlot
    rank: int
    delta: int


@dataclass(frozen=True, slots=True)
class SplitPreview:
    """Where the two clubs land after one point split."""

    label: str
    home_rank: int
    away_rank: int
    home_points: int
    away_points: int
    home_delta: int
    away_delta: int
    active: bool


@dataclass(frozen=True, slots=True)
class GroupTable:
    """Group table before this match, plus the table after its points."""

    group: str
    before: tuple[RankedSlot, ...]
    projected: tuple[RankedSlot, ...] | None
    splits: tuple[SplitPreview, ...]
    heading: str | None
    split: str | None
    moves: str | None
    captured: str | None


@dataclass(frozen=True, slots=True)
class MatchDetail:
    match: LiveMatch
    events: tuple[DetailEvent, ...]
    stats: tuple[DetailStat, ...]
    home_xi: tuple[DetailPlayer, ...]
    away_xi: tuple[DetailPlayer, ...]
    home_bench: tuple[DetailPlayer, ...]
    away_bench: tuple[DetailPlayer, ...]
    advice: str | None
    expected_home: str | None
    expected_away: str | None
    pct_draw: str | None
    comparison: tuple[tuple[str, str, str], ...]
    h2h: tuple[DetailH2H, ...]
    table: GroupTable | None = None
    markets: tuple[OddsMarket, ...] = ()
    attack_subs: tuple[AttackChange, ...] = ()
    goal_price: GoalPrice | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "match": self.match.as_dict(),
            "events": [
                {
                    "clock": item.clock,
                    "side": item.side,
                    "type": item.event_type,
                    "detail": item.detail,
                    "player": item.player,
                    "assist": item.assist,
                }
                for item in self.events
            ],
            "stats": [
                {"label": item.label, "home": item.home, "away": item.away}
                for item in self.stats
            ],
            "advice": self.advice,
            "h2h": [
                {
                    "when": item.when,
                    "home": item.home,
                    "away": item.away,
                    "score": item.score,
                }
                for item in self.h2h
            ],
        }


async def load_match_detail(engine: AsyncEngine, fixture_id: int) -> MatchDetail | None:
    match = await load_fixture_match(engine, fixture_id)
    if match is None:
        return None
    async with AsyncSession(engine, expire_on_commit=False) as session:
        events = await _events(session, match)
        stats = await _stats(session, match)
        home_xi, away_xi, home_bench, away_bench = await _lineup(session, match)
        prediction = await session.get(Prediction, fixture_id)
        h2h = await _h2h(session, fixture_id)
        table = await _group_table(session, match)
        markets = await _live_markets(session, match)
        attack_subs = await _attack_subs(session, match)
        goal_price = await _goal_price(session, match)
    advice = None
    expected_home = None
    expected_away = None
    pct_draw = None
    comparison: tuple[tuple[str, str, str], ...] = ()
    if prediction is not None:
        advice = _blank(prediction.advice)
        expected_home = _blank(prediction.goals_home)
        expected_away = _blank(prediction.goals_away)
        pct_draw = _blank(prediction.pct_draw)
        comparison = _comparison_rows(prediction.comparison)
    return MatchDetail(
        match=match,
        events=tuple(events),
        stats=tuple(stats),
        home_xi=tuple(home_xi),
        away_xi=tuple(away_xi),
        home_bench=tuple(home_bench),
        away_bench=tuple(away_bench),
        advice=advice,
        expected_home=expected_home,
        expected_away=expected_away,
        pct_draw=pct_draw,
        comparison=comparison,
        h2h=tuple(h2h),
        table=table,
        markets=tuple(markets),
        attack_subs=tuple(attack_subs),
        goal_price=goal_price,
    )


def points_split(home_goals: int, away_goals: int) -> tuple[int, int]:
    """League points from a score: 3/0, 1/1, or 0/3."""
    if home_goals > away_goals:
        return 3, 0
    if home_goals < away_goals:
        return 0, 3
    return 1, 1


def rank_slots(
    slots: tuple[StandingSlot, ...] | list[StandingSlot],
) -> tuple[RankedSlot, ...]:
    ordered = sorted(
        slots,
        key=lambda slot: (
            -slot.points,
            -slot.gd,
            -slot.gf,
            slot.api_rank,
            slot.team_id,
        ),
    )
    return tuple(
        RankedSlot(slot=slot, rank=index, delta=0)
        for index, slot in enumerate(ordered, start=1)
    )


def _shifted(
    slot: StandingSlot,
    *,
    points: int,
    gf: int,
    ga: int,
    played: int,
) -> StandingSlot:
    return StandingSlot(
        team_id=slot.team_id,
        name=slot.name,
        points=slot.points + points,
        played=slot.played + played,
        gf=slot.gf + gf,
        ga=slot.ga + ga,
        api_rank=slot.api_rank,
    )


def apply_score(
    slots: tuple[StandingSlot, ...] | list[StandingSlot],
    *,
    home_id: int,
    away_id: int,
    home_goals: int,
    away_goals: int,
    sign: int = 1,
) -> list[StandingSlot]:
    """Add (sign=1) or remove (sign=-1) this match from every group row."""
    home_points, away_points = points_split(home_goals, away_goals)
    shifted: list[StandingSlot] = []
    for slot in slots:
        if slot.team_id == home_id:
            shifted.append(
                _shifted(
                    slot,
                    points=sign * home_points,
                    gf=sign * home_goals,
                    ga=sign * away_goals,
                    played=sign,
                )
            )
        elif slot.team_id == away_id:
            shifted.append(
                _shifted(
                    slot,
                    points=sign * away_points,
                    gf=sign * away_goals,
                    ga=sign * home_goals,
                    played=sign,
                )
            )
        else:
            shifted.append(slot)
    return shifted


def _with_deltas(
    before: tuple[RankedSlot, ...], after: tuple[RankedSlot, ...]
) -> tuple[RankedSlot, ...]:
    base = {row.slot.team_id: row.rank for row in before}
    return tuple(
        RankedSlot(
            slot=row.slot,
            rank=row.rank,
            delta=base.get(row.slot.team_id, row.rank) - row.rank,
        )
        for row in after
    )


def _split_text(home: str, away: str, home_goals: int, away_goals: int) -> str:
    home_points, away_points = points_split(home_goals, away_goals)
    if home_points == away_points:
        return "Remis, po 1 punkcie"
    if home_points == 3:
        return f"{home} +3, {away} +0"
    return f"{away} +3, {home} +0"


def point_splits(
    rows: list[StandingSlot],
    *,
    home_id: int,
    away_id: int,
    home_name: str,
    away_name: str,
    home_goals: int | None,
    away_goals: int | None,
) -> tuple[SplitPreview, ...]:
    """Three outcomes from the pre-match table. The live score fills its own case."""
    before = {row.slot.team_id: row.rank for row in rank_slots(rows)}
    specs = (
        (f"Wygrana {home_name}", 1, 0),
        ("Remis", 0, 0),
        (f"Wygrana {away_name}", 0, 1),
    )
    current: int | None = None
    if home_goals is not None and away_goals is not None:
        if home_goals > away_goals:
            current = 0
        elif home_goals == away_goals:
            current = 1
        else:
            current = 2
    previews: list[SplitPreview] = []
    for index, (label, sample_home, sample_away) in enumerate(specs):
        goals_home = sample_home
        goals_away = sample_away
        active = current == index
        if active and home_goals is not None and away_goals is not None:
            goals_home = home_goals
            goals_away = away_goals
            label = f"{label} {home_goals}–{away_goals}"
        ranked = {
            row.slot.team_id: row
            for row in rank_slots(
                apply_score(
                    rows,
                    home_id=home_id,
                    away_id=away_id,
                    home_goals=goals_home,
                    away_goals=goals_away,
                )
            )
        }
        home = ranked[home_id]
        away = ranked[away_id]
        previews.append(
            SplitPreview(
                label=label,
                home_rank=home.rank,
                away_rank=away.rank,
                home_points=home.slot.points,
                away_points=away.slot.points,
                home_delta=before[home_id] - home.rank,
                away_delta=before[away_id] - away.rank,
                active=active,
            )
        )
    return tuple(previews)


def _moves(projected: tuple[RankedSlot, ...]) -> str:
    parts: list[str] = []
    for row in projected:
        if row.delta > 0:
            parts.append(f"{row.slot.name} awansuje na {row.rank}.")
        elif row.delta < 0:
            parts.append(f"{row.slot.name} spada na {row.rank}.")
    if not parts:
        return "Miejsca się nie zmieniają."
    return " ".join(parts)


def build_group_table(
    *,
    group: str,
    rows: list[StandingSlot],
    home_id: int,
    away_id: int,
    home_name: str,
    away_name: str,
    home_goals: int | None,
    away_goals: int | None,
    status_short: str,
    included: bool,
    captured: str | None,
) -> GroupTable:
    """Before-match table, and the same group after this match's points."""
    scored = home_goals is not None and away_goals is not None
    live = status_short in IN_PLAY_FIXTURE_STATUSES
    finished = status_short in FINISHED_FIXTURE_STATUSES
    project = scored and (live or finished)
    baseline = rows
    if project and included:
        assert home_goals is not None and away_goals is not None
        baseline = apply_score(
            rows,
            home_id=home_id,
            away_id=away_id,
            home_goals=home_goals,
            away_goals=away_goals,
            sign=-1,
        )
        before = rank_slots(baseline)
        projected = _with_deltas(before, rank_slots(rows))
    elif project:
        assert home_goals is not None and away_goals is not None
        before = rank_slots(rows)
        projected = _with_deltas(
            before,
            rank_slots(
                apply_score(
                    rows,
                    home_id=home_id,
                    away_id=away_id,
                    home_goals=home_goals,
                    away_goals=away_goals,
                )
            ),
        )
    else:
        before = rank_slots(rows)
        projected = None
    splits = point_splits(
        baseline,
        home_id=home_id,
        away_id=away_id,
        home_name=home_name,
        away_name=away_name,
        home_goals=home_goals if project else None,
        away_goals=away_goals if project else None,
    )
    if projected is None or home_goals is None or away_goals is None:
        return GroupTable(
            group=group,
            before=before,
            projected=None,
            splits=splits,
            heading=None,
            split=None,
            moves=None,
            captured=captured,
        )
    heading = (
        f"Przy wyniku {home_goals}–{away_goals}"
        if live
        else f"Po wyniku {home_goals}–{away_goals}"
    )
    return GroupTable(
        group=group,
        before=before,
        projected=projected,
        splits=splits,
        heading=heading,
        split=_split_text(home_name, away_name, home_goals, away_goals),
        moves=_moves(projected),
        captured=captured,
    )


def _snapshot_includes_match(
    *,
    status_short: str,
    home_played: int,
    away_played: int,
    other_finished: dict[int, int],
    home_id: int,
    away_id: int,
) -> bool:
    """True when a finished match is already inside the stored table."""
    if status_short not in FINISHED_FIXTURE_STATUSES:
        return False
    return home_played >= other_finished.get(home_id, 0) + 1 and away_played >= (
        other_finished.get(away_id, 0) + 1
    )


async def _group_table(session: AsyncSession, match: LiveMatch) -> GroupTable | None:
    rows = (
        await session.execute(
            select(Standing, Team.name)
            .join(Team, Team.id == Standing.team_id)
            .where(Standing.league_id == match.league_id)
            .where(Standing.season == match.season)
        )
    ).all()
    if not rows:
        return None
    by_group: dict[str, list[StandingSlot]] = {}
    captured_at: datetime | None = None
    for standing, name in rows:
        by_group.setdefault(standing.group_name or "", []).append(
            StandingSlot(
                team_id=int(standing.team_id),
                name=_blank(name) or "—",
                points=int(standing.points or 0),
                played=int(standing.played or 0),
                gf=int(standing.goals_for or 0),
                ga=int(standing.goals_against or 0),
                api_rank=int(standing.rank),
            )
        )
        updated = standing.api_update
        if isinstance(updated, datetime) and (
            captured_at is None or updated > captured_at
        ):
            captured_at = updated
    chosen: tuple[str, list[StandingSlot]] | None = None
    for group_name, slots in by_group.items():
        ids = {slot.team_id for slot in slots}
        if match.home_team_id in ids and match.away_team_id in ids:
            chosen = (group_name, slots)
            break
    if chosen is None:
        return None
    group_name, slots = chosen
    played = {slot.team_id: slot.played for slot in slots}
    others = (
        await session.execute(
            select(Fixture.home_team_id, Fixture.away_team_id).where(
                Fixture.league_id == match.league_id,
                Fixture.season == match.season,
                Fixture.status_short.in_(FINISHED_FIXTURE_STATUSES),
                Fixture.id != match.fixture_id,
            )
        )
    ).all()
    finished: dict[int, int] = {}
    for home_id, away_id in others:
        finished[int(home_id)] = finished.get(int(home_id), 0) + 1
        finished[int(away_id)] = finished.get(int(away_id), 0) + 1
    captured = None
    if captured_at is not None:
        captured = captured_at.strftime("%d.%m %H:%M UTC")
    return build_group_table(
        group=group_name,
        rows=slots,
        home_id=match.home_team_id,
        away_id=match.away_team_id,
        home_name=match.home,
        away_name=match.away,
        home_goals=match.goals_home,
        away_goals=match.goals_away,
        status_short=match.status_short,
        included=_snapshot_includes_match(
            status_short=match.status_short,
            home_played=played.get(match.home_team_id, 0),
            away_played=played.get(match.away_team_id, 0),
            other_finished=finished,
            home_id=match.home_team_id,
            away_id=match.away_team_id,
        ),
        captured=captured,
    )


async def _events(session: AsyncSession, match: LiveMatch) -> list[DetailEvent]:
    assist = aliased(Player)
    rows = (
        await session.execute(
            select(
                FixtureEvent.minute,
                FixtureEvent.minute_extra,
                FixtureEvent.team_id,
                FixtureEvent.event_type,
                FixtureEvent.detail,
                Player.name,
                assist.name,
                FixtureEvent.sort_order,
            )
            .outerjoin(Player, Player.id == FixtureEvent.player_id)
            .outerjoin(assist, assist.id == FixtureEvent.assist_player_id)
            .where(FixtureEvent.fixture_id == match.fixture_id)
            .order_by(
                FixtureEvent.minute,
                FixtureEvent.minute_extra.asc().nulls_first(),
                FixtureEvent.sort_order.asc().nulls_last(),
                FixtureEvent.id,
            )
        )
    ).all()
    events: list[DetailEvent] = []
    for minute, extra, team_id, event_type, detail, player, assist_name, _order in rows:
        side = "home" if int(team_id) == match.home_team_id else "away"
        events.append(
            DetailEvent(
                minute=int(minute),
                extra=extra,
                side=side,
                event_type=str(event_type),
                detail=_blank(detail),
                player=_blank(player),
                assist=_blank(assist_name),
            )
        )
    return events


_Sub = tuple[int, int | None, int, int | None, int | None, str | None, str | None]


def recent_attack_changes(
    subs: list[_Sub],
    spots: list[tuple[int, int, str | None, str | None, bool]],
    *,
    home_id: int,
    away_id: int,
    home: str,
    away: str,
    elapsed: int | None,
    extra: int | None,
) -> tuple[AttackChange, ...]:
    """Attacking substitutions in the last 15 minutes.

    API-Football stores the player coming off in ``player`` and the player
    coming on in ``assist``. A centre-forward coming on pushes the game
    forward. The only winger coming off narrows it.
    """
    roles = _starter_attack_roles(spots)
    cutoff = None if elapsed is None else max(0, elapsed - ATTACK_SUB_WINDOW)
    changes: list[AttackChange] = []
    for minute, minute_extra, team_id, off_id, on_id, off_name, on_name in subs:
        if cutoff is not None and minute < cutoff:
            continue
        off_role = _player_role(off_id, roles, spots)
        on_role = _player_role(on_id, roles, spots)
        if off_role is None and on_role is None:
            continue
        team = home if team_id == home_id else away
        summary = _sub_summary(off_name, off_role, on_name, on_role)
        effect = _sub_effect(team_id, off_id, off_role, on_role, roles, spots)
        clock = f"{minute}+{minute_extra}'" if minute_extra else f"{minute}'"
        changes.append(
            AttackChange(clock=clock, team=team, summary=summary, effect=effect)
        )
    return tuple(changes)


def _starter_attack_roles(
    spots: list[tuple[int, int, str | None, str | None, bool]],
) -> dict[int, str]:
    roles: dict[int, str] = {}
    by_team: dict[int, list[tuple[int, int, int]]] = {}
    for team_id, player_id, position, grid, starter in spots:
        if not starter or not _is_forward(position):
            continue
        parsed = _parse_grid(grid)
        if parsed is None:
            continue
        by_team.setdefault(team_id, []).append((parsed[0], parsed[1], player_id))
    for players in by_team.values():
        front = max(row for row, _col, _pid in players)
        line = [(col, pid) for row, col, pid in players if row == front]
        if len(line) >= 3:
            edge = {min(line)[1], max(line)[1]}
            for _col, pid in line:
                roles[pid] = "wing" if pid in edge else "cf"
            continue
        if len(line) == 1:
            roles[line[0][1]] = "cf"
            support = [(col, pid) for row, col, pid in players if row == front - 1]
            for _col, pid in support:
                roles[pid] = "wing"
            continue
        for _col, pid in line:
            roles[pid] = "cf"
    return roles


def _player_role(
    player_id: int | None,
    roles: dict[int, str],
    spots: list[tuple[int, int, str | None, str | None, bool]],
) -> str | None:
    if player_id is None:
        return None
    role = roles.get(player_id)
    if role == "wing":
        return "skrzydłowy"
    if role == "cf":
        return "środkowy napastnik"
    for _team, pid, position, _grid, _starter in spots:
        if pid == player_id and _is_forward(position):
            return "napastnik"
    return None


def _sub_effect(
    team_id: int,
    off_id: int | None,
    off_role: str | None,
    on_role: str | None,
    roles: dict[int, str],
    spots: list[tuple[int, int, str | None, str | None, bool]],
) -> str | None:
    notes: list[str] = []
    if on_role in {"napastnik", "środkowy napastnik"}:
        notes.append("Wszedł napastnik — więcej gry do przodu.")
    team_wings = [
        player_id
        for spot_team, player_id, _position, _grid, _starter in spots
        if spot_team == team_id and roles.get(player_id) == "wing"
    ]
    only_wing_off = (
        off_role == "skrzydłowy"
        and off_id is not None
        and team_wings == [off_id]
        and on_role != "skrzydłowy"
    )
    if only_wing_off:
        notes.append("Zeszło jedyne skrzydło — mniej szerokości.")
    if not notes:
        return None
    return " ".join(notes)


def _is_forward(position: str | None) -> bool:
    if not position:
        return False
    return position.strip().upper().startswith("F")


def _parse_grid(grid: str | None) -> tuple[int, int] | None:
    if not grid or ":" not in grid:
        return None
    row_raw, col_raw = grid.split(":", 1)
    try:
        return int(row_raw), int(col_raw)
    except ValueError:
        return None


def _sub_summary(
    off_name: str | None,
    off_role: str | None,
    on_name: str | None,
    on_role: str | None,
) -> str:
    parts: list[str] = []
    if off_name:
        role = f" ({off_role})" if off_role else ""
        parts.append(f"schodzi {off_name}{role}")
    if on_name:
        role = f" ({on_role})" if on_role else ""
        parts.append(f"wchodzi {on_name}{role}")
    return ", ".join(parts)


async def _attack_subs(session: AsyncSession, match: LiveMatch) -> list[AttackChange]:
    assist = aliased(Player)
    rows = (
        await session.execute(
            select(
                FixtureEvent.minute,
                FixtureEvent.minute_extra,
                FixtureEvent.team_id,
                FixtureEvent.player_id,
                FixtureEvent.assist_player_id,
                Player.name,
                assist.name,
            )
            .outerjoin(Player, Player.id == FixtureEvent.player_id)
            .outerjoin(assist, assist.id == FixtureEvent.assist_player_id)
            .where(FixtureEvent.fixture_id == match.fixture_id)
            .where(func.lower(FixtureEvent.event_type) == "subst")
            .order_by(
                FixtureEvent.minute,
                FixtureEvent.minute_extra.asc().nulls_first(),
            )
        )
    ).all()
    spots = (
        await session.execute(
            select(
                FixtureLineupPlayer.team_id,
                FixtureLineupPlayer.player_id,
                FixtureLineupPlayer.position,
                FixtureLineupPlayer.grid,
                FixtureLineupPlayer.is_starter,
            ).where(FixtureLineupPlayer.fixture_id == match.fixture_id)
        )
    ).all()
    subs = [
        (
            int(minute),
            extra,
            int(team_id),
            None if off_id is None else int(off_id),
            None if on_id is None else int(on_id),
            _blank(off_name),
            _blank(on_name),
        )
        for minute, extra, team_id, off_id, on_id, off_name, on_name in rows
    ]
    lineup = [
        (
            int(team_id),
            int(player_id),
            _blank(position),
            _blank(grid),
            bool(starter),
        )
        for team_id, player_id, position, grid, starter in spots
    ]
    return list(
        recent_attack_changes(
            subs,
            lineup,
            home_id=match.home_team_id,
            away_id=match.away_team_id,
            home=match.home,
            away=match.away,
            elapsed=match.elapsed,
            extra=match.extra,
        )
    )


async def _stats(session: AsyncSession, match: LiveMatch) -> list[DetailStat]:
    rows = (
        await session.execute(
            select(
                FixtureStatistic.team_id,
                FixtureStatistic.stat_type,
                FixtureStatistic.stat_value,
            ).where(
                FixtureStatistic.fixture_id == match.fixture_id,
                FixtureStatistic.period == "FT",
            )
        )
    ).all()
    paired: dict[str, dict[str, str | None]] = {}
    for team_id, stat_type, value in rows:
        side = "home" if int(team_id) == match.home_team_id else "away"
        paired.setdefault(str(stat_type), {"home": None, "away": None})[side] = _blank(
            value
        )
    ordered = [name for name in _STAT_ORDER if name in paired]
    ordered.extend(sorted(name for name in paired if name not in _STAT_ORDER))
    return [
        DetailStat(
            label=_STAT_LABELS.get(name, name),
            home=paired[name]["home"],
            away=paired[name]["away"],
        )
        for name in ordered
    ]


async def _lineup(session: AsyncSession, match: LiveMatch) -> tuple[
    list[DetailPlayer],
    list[DetailPlayer],
    list[DetailPlayer],
    list[DetailPlayer],
]:
    rows = (
        await session.execute(
            select(
                FixtureLineupPlayer.team_id,
                FixtureLineupPlayer.number,
                FixtureLineupPlayer.position,
                FixtureLineupPlayer.grid,
                FixtureLineupPlayer.is_starter,
                Player.name,
            )
            .outerjoin(Player, Player.id == FixtureLineupPlayer.player_id)
            .where(FixtureLineupPlayer.fixture_id == match.fixture_id)
        )
    ).all()
    home_xi: list[DetailPlayer] = []
    away_xi: list[DetailPlayer] = []
    home_bench: list[DetailPlayer] = []
    away_bench: list[DetailPlayer] = []
    keyed: list[tuple[str, bool, str, DetailPlayer]] = []
    for team_id, number, position, grid, starter, name in rows:
        side = "home" if int(team_id) == match.home_team_id else "away"
        player = DetailPlayer(
            name=_blank(name) or "—",
            number=number,
            position=_blank(position),
            starter=bool(starter),
        )
        keyed.append((side, bool(starter), str(grid or ""), player))
    keyed.sort(
        key=lambda item: (
            item[0],
            not item[1],
            _grid_key(item[2]),
            item[3].number or 99,
        )
    )
    for side, starter, _grid, player in keyed:
        if side == "home":
            (home_xi if starter else home_bench).append(player)
        else:
            (away_xi if starter else away_bench).append(player)
    return home_xi, away_xi, home_bench, away_bench


async def _h2h(session: AsyncSession, fixture_id: int) -> list[DetailH2H]:
    home = aliased(Team)
    away = aliased(Team)
    rows = (
        await session.execute(
            select(
                Fixture.date,
                home.name,
                away.name,
                Fixture.goals_home,
                Fixture.goals_away,
                PredictionH2H.sort_order,
            )
            .join(Fixture, Fixture.id == PredictionH2H.h2h_fixture_id)
            .join(home, home.id == Fixture.home_team_id)
            .join(away, away.id == Fixture.away_team_id)
            .where(PredictionH2H.fixture_id == fixture_id)
            .order_by(
                PredictionH2H.sort_order.asc().nulls_last(),
                Fixture.date.desc().nulls_last(),
            )
            .limit(8)
        )
    ).all()
    items: list[DetailH2H] = []
    for kickoff, home_name, away_name, goals_home, goals_away, _order in rows:
        when = ""
        if isinstance(kickoff, datetime):
            when = kickoff.date().isoformat()
        score = "–"
        if goals_home is not None and goals_away is not None:
            score = f"{goals_home}–{goals_away}"
        items.append(
            DetailH2H(
                when=when,
                home=_blank(home_name) or "—",
                away=_blank(away_name) or "—",
                score=score,
            )
        )
    return items


def _comparison_rows(raw: object) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(raw, dict):
        return ()
    rows: list[tuple[str, str, str]] = []
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        home = value.get("home")
        away = value.get("away")
        if home is None and away is None:
            continue
        label = _COMPARE_LABELS.get(str(key), str(key))
        rows.append((label, str(home or "—"), str(away or "—")))
    return tuple(rows)


def _blank(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _grid_key(grid: str) -> tuple[int, int]:
    parts = grid.split(":")
    if len(parts) != 2:
        return (99, 99)
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return (99, 99)


def _num(raw: str | None) -> float | None:
    if raw is None:
        return None
    cleaned = raw.strip().rstrip("%").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def render_match_missing() -> str:
    return _page(
        title="Mecz",
        body='<p class="empty">Nie ma takiego meczu w bazie.</p>',
    )


def render_match_html(detail: MatchDetail, *, generated_at: datetime) -> str:
    match = detail.match
    title = f"{match.home} – {match.away}"
    return _page(title=title, body=_body(detail, generated_at), refresh=True)


def _page(*, title: str, body: str, refresh: bool = False) -> str:
    script = ""
    if refresh:
        script = f"""
<script>
setTimeout(function () {{ location.reload(); }}, {REFRESH_SECONDS * 1000});
</script>
"""
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
{_DETAIL_CSS}
</style>
</head>
<body>
<header>
  <h1><span class="dot" aria-hidden="true"></span> {escape(title)}</h1>
  <div class="header-meta">
    {_live_nav("all")}
  </div>
</header>
<main>
  {body}
</main>
{script}
</body>
</html>
"""


def _body(detail: MatchDetail, generated_at: datetime) -> str:
    match = detail.match
    chips = _chips(match)
    periods = _periods(match)
    return f"""
<section class="hero">
  <p class="kicker">{escape(match.country)} · {escape(match.league)}</p>
  <div class="headline">
    <div class="team home">{_img(match.home_logo, match.home)}
      <span>{escape(match.home)}</span></div>
    <div class="scoreblock">
      <div class="score">{_score(match.goals_home)}–{_score(match.goals_away)}</div>
      <div class="meta"><span class="clock">{escape(match.clock)}</span>
        <span>{escape(match.status_short)}</span></div>
    </div>
    <div class="team away"><span>{escape(match.away)}</span>
      {_img(match.away_logo, match.away)}</div>
  </div>
  {chips}
  {periods}
</section>
{_table_card(detail)}
<div class="grid">
  {_events_card(detail)}
  {_stats_card(detail)}
</div>
{_attack_subs_card(detail)}
{_goal_price_card(detail)}
{_lineups_card(detail)}
{_odds_card(detail)}
{_prediction_card(detail)}
{_h2h_card(detail)}
<p class="sub">Odświeżono {escape(generated_at.strftime("%H:%M:%S UTC"))}</p>
"""


def _chips(match: LiveMatch) -> str:
    bits: list[str] = []
    if match.round:
        bits.append(match.round)
    venue = " · ".join(part for part in (match.venue, match.venue_city) if part)
    if venue:
        bits.append(venue)
    if match.referee:
        bits.append(match.referee)
    if match.kickoff is not None:
        bits.append(match.kickoff.strftime("%Y-%m-%d %H:%M UTC"))
    if match.home_formation or match.away_formation:
        bits.append(f"{match.home_formation or '—'} / {match.away_formation or '—'}")
    if not bits:
        return ""
    return (
        '<p class="chips">'
        + "".join(f"<span>{escape(bit)}</span>" for bit in bits)
        + "</p>"
    )


def _periods(match: LiveMatch) -> str:
    cells: list[str] = []
    if match.ht_home is not None and match.ht_away is not None:
        cells.append(f"<span>HT {match.ht_home}–{match.ht_away}</span>")
    if match.et_home is not None and match.et_away is not None:
        cells.append(f"<span>ET {match.et_home}–{match.et_away}</span>")
    if match.pen_home is not None and match.pen_away is not None:
        cells.append(f"<span>KARNE {match.pen_home}–{match.pen_away}</span>")
    if not cells:
        return ""
    return f'<p class="periods">{"".join(cells)}</p>'


def _events_card(detail: MatchDetail) -> str:
    if not detail.events:
        inner = '<p class="muted">Brak zdarzeń.</p>'
    else:
        rows = []
        for event in detail.events:
            who = event.player or ""
            if event.assist and event.event_type == "Goal":
                who = f"{who} ({event.assist})" if who else event.assist
            label = _EVENT_LABELS.get(event.event_type, event.event_type)
            detail_text = event.detail or ""
            side = "home" if event.side == "home" else "away"
            rows.append(
                f'<li class="{side}">'
                f'<span class="minute">{escape(event.clock)}</span>'
                f'<span class="what">{escape(label)}'
                + (f" <em>{escape(detail_text)}</em>" if detail_text else "")
                + f'</span><span class="who">{escape(who)}</span></li>'
            )
        inner = f'<ol class="timeline">{"".join(rows)}</ol>'
    return f'<section class="card"><h2>Zdarzenia</h2>{inner}</section>'

    return f'<section class="card"><h2>Zdarzenia</h2>{inner}</section>'


def _attack_subs_card(detail: MatchDetail) -> str:
    if not detail.attack_subs:
        return ""
    rows = []
    for change in detail.attack_subs:
        effect = ""
        if change.effect:
            effect = f'<p class="effect">{escape(change.effect)}</p>'
        rows.append(
            "<li>"
            f'<span class="minute">{escape(change.clock)}</span>'
            f"<span><strong>{escape(change.team)}</strong> "
            f"{escape(change.summary)}</span>"
            f"{effect}</li>"
        )
    return (
        '<section class="card"><h2>Zmiany napastników</h2>'
        f'<ul class="subs">{"".join(rows)}</ul></section>'
    )


def _stats_card(detail: MatchDetail) -> str:
    if not detail.stats:
        inner = '<p class="muted">Brak statystyk.</p>'
    else:
        rows = []
        for stat in detail.stats:
            home_n = _num(stat.home)
            away_n = _num(stat.away)
            home_pct = 50.0
            away_pct = 50.0
            if home_n is not None and away_n is not None and (home_n + away_n) > 0:
                home_pct = 100.0 * home_n / (home_n + away_n)
                away_pct = 100.0 - home_pct
            rows.append(
                "<div class='stat'>"
                f"<div class='stat-top'><span>{escape(stat.home or '—')}</span>"
                f"<span class='stat-label'>{escape(stat.label)}</span>"
                f"<span>{escape(stat.away or '—')}</span></div>"
                "<div class='bars'>"
                f"<i style='width:{home_pct:.1f}%'></i>"
                f"<i class='away' style='width:{away_pct:.1f}%'></i>"
                "</div></div>"
            )
        inner = "".join(rows)
    return f'<section class="card"><h2>Statystyki</h2>{inner}</section>'


def _lineups_card(detail: MatchDetail) -> str:
    if not any(
        (
            detail.home_xi,
            detail.away_xi,
            detail.home_bench,
            detail.away_bench,
        )
    ):
        return (
            '<section class="card"><h2>Składy</h2>'
            '<p class="muted">Brak składów.</p></section>'
        )
    match = detail.match
    return f"""
<section class="card">
  <h2>Składy</h2>
  <div class="xi">
    <div>
      <h3>{escape(match.home)}</h3>
      {_player_list(detail.home_xi)}
      {_bench(detail.home_bench)}
    </div>
    <div>
      <h3>{escape(match.away)}</h3>
      {_player_list(detail.away_xi)}
      {_bench(detail.away_bench)}
    </div>
  </div>
</section>
"""


def _player_list(players: tuple[DetailPlayer, ...]) -> str:
    if not players:
        return '<p class="muted">—</p>'
    items = []
    for player in players:
        number = "" if player.number is None else f"{player.number}"
        pos = "" if not player.position else f" {escape(player.position)}"
        items.append(
            f"<li><span class='num'>{escape(number)}</span>"
            f"<span>{escape(player.name)}</span>"
            f"<span class='pos'>{pos}</span></li>"
        )
    return f"<ul class='xi-list'>{''.join(items)}</ul>"


def _bench(players: tuple[DetailPlayer, ...]) -> str:
    if not players:
        return ""
    names = ", ".join(
        (
            escape(player.name)
            if player.number is None
            else f"{player.number} {escape(player.name)}"
        )
        for player in players
    )
    return f"<p class='bench'><span>Ławka</span> {names}</p>"


def _table_card(detail: MatchDetail) -> str:
    table = detail.table
    if table is None:
        return (
            '<section class="card"><h2>Tabela</h2>'
            '<p class="muted">Brak tabeli dla tej pary.</p></section>'
        )
    match = detail.match
    caption = escape(table.group) if table.group else "Grupa"
    if table.captured:
        caption = f"{caption} · stan z {escape(table.captured)}"
    live = ""
    if table.projected is not None and table.heading:
        note = ""
        if table.split:
            note += f'<p class="split">{escape(table.split)}</p>'
        if table.moves:
            note += f'<p class="moves">{escape(table.moves)}</p>'
        live = (
            "<div>"
            f"<h3>{escape(table.heading)}</h3>"
            f"{note}"
            + _standings_table(
                table.projected,
                home_id=match.home_team_id,
                away_id=match.away_team_id,
                show_delta=True,
            )
            + "</div>"
        )
    return (
        '<section class="card">'
        "<h2>Tabela</h2>"
        f'<p class="kicker">{caption}</p>'
        '<div class="tables">'
        "<div><h3>Przed meczem</h3>"
        + _standings_table(
            table.before,
            home_id=match.home_team_id,
            away_id=match.away_team_id,
            show_delta=False,
        )
        + "</div>"
        + live
        + "</div>"
        + _split_cards(table, match)
        + "</section>"
    )


def _split_cards(table: GroupTable, match: LiveMatch) -> str:
    if not table.splits:
        return ""
    cards: list[str] = []
    for item in table.splits:
        kind = "split-card active" if item.active else "split-card"
        cards.append(
            f'<div class="{kind}">'
            f'<p class="split-label">{escape(item.label)}</p>'
            "<p>"
            f"{escape(match.home)} {item.home_rank}. · {item.home_points} pkt "
            f"{_delta(item.home_delta)}</p><p>"
            f"{escape(match.away)} {item.away_rank}. · {item.away_points} pkt "
            f"{_delta(item.away_delta)}</p></div>"
        )
    return (
        '<h3 class="split-title">Podział punktów</h3>'
        '<div class="split-grid">' + "".join(cards) + "</div>"
    )


def _standings_table(
    rows: tuple[RankedSlot, ...],
    *,
    home_id: int,
    away_id: int,
    show_delta: bool,
) -> str:
    head_delta = "<th></th>" if show_delta else ""
    body: list[str] = []
    for row in rows:
        slot = row.slot
        classes: list[str] = []
        if slot.team_id in {home_id, away_id}:
            classes.append("club")
        if show_delta and row.delta > 0:
            classes.append("up")
        elif show_delta and row.delta < 0:
            classes.append("down")
        class_attr = f' class="{" ".join(classes)}"' if classes else ""
        delta = f"<td>{_delta(row.delta)}</td>" if show_delta else ""
        gd = f"{slot.gd:+d}"
        body.append(
            f"<tr{class_attr}>"
            f"<td>{row.rank}</td>"
            f'<td class="name">{escape(slot.name)}</td>'
            f"<td>{slot.played}</td>"
            f"<td>{slot.points}</td>"
            f"<td>{slot.gf}–{slot.ga}</td>"
            f"<td>{gd}</td>"
            f"{delta}</tr>"
        )
    return (
        '<table class="stand"><thead><tr>'
        "<th>#</th><th class='name'>Drużyna</th><th>M</th><th>Pkt</th>"
        f"<th>Bramki</th><th>+/-</th>{head_delta}"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def _delta(delta: int) -> str:
    if delta > 0:
        return f'<span class="delta up">↑{delta}</span>'
    if delta < 0:
        return f'<span class="delta down">↓{-delta}</span>'
    return '<span class="delta flat">·</span>'


def _prediction_card(detail: MatchDetail) -> str:
    match = detail.match
    home_pct = match.prediction_pct_home
    away_pct = match.prediction_pct_away
    has_pct = home_pct is not None or away_pct is not None or detail.pct_draw
    if (
        not has_pct
        and not detail.advice
        and not detail.comparison
        and not detail.expected_home
    ):
        return ""
    bits: list[str] = []
    if has_pct:
        bits.append(
            "<p class='pct'>"
            f"<span>{_pct(home_pct)}</span>"
            f"<span>Remis {_pct_text(detail.pct_draw)}</span>"
            f"<span>{_pct(away_pct)}</span></p>"
        )
    if detail.expected_home or detail.expected_away:
        bits.append(
            "<p class='muted'>Oczekiwane gole "
            f"{escape(detail.expected_home or '—')} – "
            f"{escape(detail.expected_away or '—')}</p>"
        )
    if detail.advice:
        bits.append(f"<p class='advice'>{escape(detail.advice)}</p>")
    if detail.comparison:
        rows = "".join(
            "<li><span>"
            + escape(home)
            + "</span><span class='stat-label'>"
            + escape(label)
            + "</span><span>"
            + escape(away)
            + "</span></li>"
            for label, home, away in detail.comparison
        )
        bits.append(f"<ul class='compare'>{rows}</ul>")
    return f'<section class="card"><h2>Prognoza</h2>{"".join(bits)}</section>'


@dataclass(frozen=True, slots=True)
class OddsQuote:
    label: str
    odd: str


@dataclass(frozen=True, slots=True)
class OddsMarket:
    title: str
    quotes: tuple[OddsQuote, ...]


_ODDS_MARKETS = (
    ("fulltime result", "Wynik"),
    ("double chance", "Podwójna szansa"),
    ("both teams to score", "Obie strzelą"),
    ("over/under line", "Powyżej / poniżej"),
    ("match goals", "Liczba goli"),
    ("asian handicap", "Handicap"),
)


def select_display_markets(
    rows: list[tuple[str, str, str, Decimal, bool | None, bool | None]],
    *,
    home: str,
    away: str,
    goals_home: int | None,
    goals_away: int | None,
) -> tuple[OddsMarket, ...]:
    """Latest live prices for the markets useful on the match page."""
    grouped: dict[str, list[tuple[str, str, Decimal, bool | None]]] = {}
    for market, label, handicap, odd, is_main, suspended in rows:
        if suspended:
            continue
        key = market.strip().casefold()
        grouped.setdefault(key, []).append((label, handicap, odd, is_main))
    markets: list[OddsMarket] = []
    for key, title in _ODDS_MARKETS:
        lines = grouped.get(key)
        if not lines:
            continue
        chosen = _pick_lines(key, lines)
        quotes = tuple(
            OddsQuote(
                label=_price_label(label, handicap, home, away),
                odd=_fmt_odd(odd),
            )
            for label, handicap, odd, _main in chosen
        )
        quotes = tuple(quote for quote in quotes if quote.label)
        if quotes:
            markets.append(OddsMarket(title=title, quotes=quotes))
    nxt = _next_goal_market(grouped, home, away, goals_home, goals_away)
    if nxt is not None:
        markets.append(nxt)
    return tuple(markets)


def _pick_lines(
    market: str,
    lines: list[tuple[str, str, Decimal, bool | None]],
) -> list[tuple[str, str, Decimal, bool | None]]:
    mains = [line for line in lines if line[3] is True]
    chosen = mains or lines
    if not mains and market == "match goals":
        chosen = [line for line in lines if _handicap_num(line[1]) in {1.5, 2.5, 3.5}]
    return sorted(chosen, key=lambda line: _line_order(line[0], line[1]))


def _line_order(label: str, handicap: str) -> tuple[int, float, str]:
    folded = label.strip().casefold()
    rank = {
        "home": 0,
        "over": 0,
        "yes": 0,
        "home or draw": 0,
        "draw or home": 0,
        "draw": 1,
        "home or away": 1,
        "away or home": 1,
        "away": 2,
        "under": 2,
        "no": 2,
        "away or draw": 2,
        "draw or away": 2,
    }.get(folded, 3)
    return (rank, _handicap_num(handicap) or 0.0, folded)


def _next_goal_market(
    grouped: dict[str, list[tuple[str, str, Decimal, bool | None]]],
    home: str,
    away: str,
    goals_home: int | None,
    goals_away: int | None,
) -> OddsMarket | None:
    scored = (goals_home or 0) + (goals_away or 0)
    target = f"which team will score the {_ordinal(scored + 1)} goal?"
    lines = grouped.get(target)
    if not lines:
        return None
    quotes = tuple(
        OddsQuote(label=_price_label(label, handicap, home, away), odd=_fmt_odd(odd))
        for label, handicap, odd, _main in lines
    )
    if not quotes:
        return None
    return OddsMarket(title=f"Kto strzeli {scored + 1}. gola", quotes=quotes)


def _ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _price_label(label: str, handicap: str, home: str, away: str) -> str:
    folded = label.strip().casefold()
    names = {
        "home": home,
        "away": away,
        "draw": "Remis",
        "yes": "Tak",
        "no": "Nie",
        "over": "Powyżej",
        "under": "Poniżej",
        "home or draw": "1X",
        "draw or home": "1X",
        "away or draw": "X2",
        "draw or away": "X2",
        "home or away": "12",
        "away or home": "12",
    }
    text = names.get(folded, label.strip())
    line = handicap.strip()
    if line and line not in {"0", "0.0"}:
        return f"{text} {line}"
    return text


def _handicap_num(raw: str) -> float | None:
    text = raw.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _fmt_odd(raw: Decimal) -> str:
    return f"{raw.quantize(Decimal('0.01')):.2f}"


async def _live_markets(session: AsyncSession, match: LiveMatch) -> list[OddsMarket]:
    latest = await session.scalar(
        select(func.max(FixtureOddsLive.captured_at)).where(
            FixtureOddsLive.fixture_id == match.fixture_id
        )
    )
    if latest is None:
        return []
    rows = (
        await session.execute(
            select(
                OddsLiveBet.name,
                FixtureOddsLive.value_label,
                FixtureOddsLive.handicap,
                FixtureOddsLive.odd,
                FixtureOddsLive.is_main,
                FixtureOddsLive.suspended,
            )
            .join(OddsLiveBet, OddsLiveBet.id == FixtureOddsLive.bet_id)
            .where(FixtureOddsLive.fixture_id == match.fixture_id)
            .where(FixtureOddsLive.captured_at == latest)
        )
    ).all()
    parsed: list[tuple[str, str, str, Decimal, bool | None, bool | None]] = []
    for market, label, handicap, odd, is_main, suspended in rows:
        if not market or not label:
            continue
        parsed.append(
            (
                str(market),
                str(label),
                "" if handicap is None else str(handicap),
                Decimal(odd),
                is_main,
                suspended,
            )
        )
    return list(
        select_display_markets(
            parsed,
            home=match.home,
            away=match.away,
            goals_home=match.goals_home,
            goals_away=match.goals_away,
        )
    )


_NONE_LABELS = frozenset({"no goal", "none", "no", "neither", "no goals"})
_OVER_MARKETS = frozenset({"over/under line", "match goals"})


def read_goal_price(
    *,
    none_odd: Decimal | None,
    over_odd: Decimal | None,
    line: float,
    ht_odd: Decimal | None,
    ht_elapsed: int | None,
) -> GoalPrice | None:
    """No-goal price and over current total + 0.5 are the same bet."""
    if none_odd is None and over_odd is None:
        return None
    return GoalPrice(
        none_odd=None if none_odd is None else _fmt_odd(none_odd),
        none_implied=None if none_odd is None else _implied(none_odd),
        line=_line_label(line),
        over_odd=None if over_odd is None else _fmt_odd(over_odd),
        over_implied=None if over_odd is None else _implied(over_odd),
        move=_line_move(over_odd, ht_odd, ht_elapsed),
    )


def _implied(odd: Decimal) -> str:
    if odd <= 0:
        return "—"
    return f"{(Decimal(100) / odd).quantize(Decimal('1')):.0f}%"


def _line_label(line: float) -> str:
    text = f"{line:.1f}"
    return text.replace(".", ",")


def _line_move(
    now_odd: Decimal | None,
    ht_odd: Decimal | None,
    ht_elapsed: int | None,
) -> str | None:
    if now_odd is None or ht_odd is None or now_odd == ht_odd:
        return None
    if ht_elapsed is None or ht_elapsed <= 50:
        start = "od przerwy"
    else:
        start = f"od {ht_elapsed}′"
    change = f"{start} {_fmt_odd(ht_odd)} → {_fmt_odd(now_odd)}"
    if now_odd < ht_odd:
        return f"{change}. Rynek widzi więcej sytuacji."
    return f"{change}. Rynek widzi mniej sytuacji."


async def _goal_price(session: AsyncSession, match: LiveMatch) -> GoalPrice | None:
    total = (match.goals_home or 0) + (match.goals_away or 0)
    line = total + 0.5
    latest = await session.scalar(
        select(func.max(FixtureOddsLive.captured_at)).where(
            FixtureOddsLive.fixture_id == match.fixture_id
        )
    )
    none_odd = _none_from_next_goal(match)
    over_odd = None
    if latest is not None:
        rows = (
            await session.execute(
                select(
                    OddsLiveBet.name,
                    FixtureOddsLive.value_label,
                    FixtureOddsLive.handicap,
                    FixtureOddsLive.odd,
                    FixtureOddsLive.suspended,
                )
                .join(OddsLiveBet, OddsLiveBet.id == FixtureOddsLive.bet_id)
                .where(FixtureOddsLive.fixture_id == match.fixture_id)
                .where(FixtureOddsLive.captured_at == latest)
            )
        ).all()
        if none_odd is None:
            none_odd = _none_odd(rows, total)
        over_odd = _over_odd(rows, line)
    ht_odd, ht_elapsed = await _ht_over(session, match.fixture_id, line, total)
    return read_goal_price(
        none_odd=none_odd,
        over_odd=over_odd,
        line=line,
        ht_odd=ht_odd,
        ht_elapsed=ht_elapsed,
    )


def _none_from_next_goal(match: LiveMatch) -> Decimal | None:
    quote = match.next_goal
    if quote is None or not quote.none:
        return None
    try:
        return Decimal(quote.none)
    except Exception:
        return None


def _none_odd(rows: Sequence[Any], total: int) -> Decimal | None:
    target = f"which team will score the {_ordinal(total + 1)} goal?"
    for market, label, _handicap, odd, suspended in rows:
        if suspended or not market or not label:
            continue
        if str(market).strip().casefold() != target:
            continue
        if str(label).strip().casefold() in _NONE_LABELS:
            return Decimal(odd)
    return None


def _over_odd(rows: Sequence[Any], line: float) -> Decimal | None:
    for market, label, handicap, odd, suspended in rows:
        if suspended or not market or not label:
            continue
        if str(market).strip().casefold() not in _OVER_MARKETS:
            continue
        if str(label).strip().casefold() != "over":
            continue
        number = _handicap_num("" if handicap is None else str(handicap))
        if number is not None and abs(number - line) < 0.01:
            return Decimal(odd)
    return None


async def _ht_over(
    session: AsyncSession,
    fixture_id: int,
    line: float,
    total: int,
) -> tuple[Decimal | None, int | None]:
    rows = (
        await session.execute(
            select(
                FixtureOddsLive.odd,
                FixtureOddsLive.handicap,
                FixtureOddsLive.elapsed_minutes,
            )
            .join(OddsLiveBet, OddsLiveBet.id == FixtureOddsLive.bet_id)
            .where(FixtureOddsLive.fixture_id == fixture_id)
            .where(func.lower(OddsLiveBet.name).in_(tuple(_OVER_MARKETS)))
            .where(func.lower(FixtureOddsLive.value_label) == "over")
            .where(FixtureOddsLive.elapsed_minutes >= 45)
            .where(FixtureOddsLive.home_goals + FixtureOddsLive.away_goals == total)
            .order_by(FixtureOddsLive.captured_at.asc())
        )
    ).all()
    for odd, handicap, elapsed in rows:
        number = _handicap_num("" if handicap is None else str(handicap))
        if number is None or abs(number - line) >= 0.01:
            continue
        return Decimal(odd), None if elapsed is None else int(elapsed)
    return None, None


def _goal_price_card(detail: MatchDetail) -> str:
    price = detail.goal_price
    if price is None:
        return ""
    bits: list[str] = []
    if price.none_odd:
        bits.append(
            "<p class='chance'>"
            f"<span class='stat-label'>Brak gola</span>"
            f"<strong>{escape(price.none_odd)}</strong>"
            f"<span>{escape(price.none_implied or '')} z kursu</span></p>"
        )
    if price.over_odd:
        bits.append(
            "<p class='chance'>"
            f"<span class='stat-label'>Powyżej {escape(price.line)}</span>"
            f"<strong>{escape(price.over_odd)}</strong>"
            f"<span>{escape(price.over_implied or '')} z kursu</span></p>"
            "<p class='muted'>To ten sam zakład co „padnie jeszcze gol”.</p>"
        )
    if price.move:
        bits.append(f"<p class='effect'>{escape(price.move)}</p>")
    if not bits:
        return ""
    return f'<section class="card"><h2>Czy padnie gol</h2>{"".join(bits)}</section>'


def _odds_card(detail: MatchDetail) -> str:
    match = detail.match
    blocks: list[str] = []
    prematch = match.prematch
    if prematch is not None and prematch.source == "prematch":
        blocks.append(
            _odd_row(
                "Otwarcie",
                match.home,
                prematch.home,
                "Remis",
                prematch.draw,
                match.away,
                prematch.away,
            )
        )
    if detail.markets:
        blocks.extend(_market_block(market) for market in detail.markets)
    else:
        if prematch is not None and prematch.source != "prematch":
            blocks.append(
                _odd_row(
                    "Otwarcie live",
                    match.home,
                    prematch.home,
                    "Remis",
                    prematch.draw,
                    match.away,
                    prematch.away,
                )
            )
        next_goal = match.next_goal
        if next_goal is not None:
            blocks.append(
                _odd_row(
                    "Następna bramka",
                    match.home,
                    next_goal.home,
                    "Brak",
                    next_goal.none,
                    match.away,
                    next_goal.away,
                )
            )
    form = _form(match)
    if form:
        blocks.append(form)
    if not blocks:
        return ""
    return f'<section class="card"><h2>Kursy</h2>{"".join(blocks)}</section>'


def _market_block(market: OddsMarket) -> str:
    chips = "".join(
        f"<span><em>{escape(quote.label)}</em> {escape(quote.odd)}</span>"
        for quote in market.quotes
    )
    return (
        f"<div class='market'><h3>{escape(market.title)}</h3>"
        f"<div class='quotes'>{chips}</div></div>"
    )


def _odd_row(
    label: str,
    home_name: str,
    home: str | None,
    mid_name: str,
    mid: str | None,
    away_name: str,
    away: str | None,
) -> str:
    cells = []
    for name, odd in ((home_name, home), (mid_name, mid), (away_name, away)):
        if odd is None:
            continue
        cells.append(f"<span><em>{escape(name)}</em> {escape(odd)}</span>")
    if not cells:
        return ""
    return (
        f"<p class='odds'><span class='stat-label'>{escape(label)}</span>"
        f"{''.join(cells)}</p>"
    )


def _form(match: LiveMatch) -> str:
    parts: list[str] = []
    for name, form in ((match.home, match.form_home), (match.away, match.form_away)):
        if form is None:
            continue
        minute = "" if form.minute_label is None else f", gol {form.minute_label}"
        sample = "" if form.matches == FORM_LAST_MATCHES else f" ({form.matches} m.)"
        parts.append(
            f"{escape(name)} {form.avg_goals:.2f} gola{minute}{escape(sample)}"
        )
    if not parts:
        return ""
    return (
        f'<p class="muted">Ostatnie {FORM_LAST_MATCHES} meczów · '
        f"{' · '.join(parts)}</p>"
    )


def _h2h_card(detail: MatchDetail) -> str:
    if not detail.h2h:
        return ""
    rows = "".join(
        "<li><span class='when'>"
        + escape(item.when)
        + "</span><span>"
        + escape(item.home)
        + " "
        + escape(item.score)
        + " "
        + escape(item.away)
        + "</span></li>"
        for item in detail.h2h
    )
    return (
        '<section class="card"><h2>Bezpośrednie</h2>'
        f'<ul class="h2h">{rows}</ul></section>'
    )


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}%"


def _pct_text(value: str | None) -> str:
    if not value:
        return "—"
    text = value.strip()
    return text if text.endswith("%") else f"{text}%"


_DETAIL_CSS = """
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --line: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --live: #3fb950;
    --score: #f0f6fc;
    --bar: #58a6ff;
    --bar-away: #8b949e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.45;
  }
  header {
    position: sticky;
    top: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 12px 24px;
    align-items: baseline;
    justify-content: space-between;
    padding: 16px 20px;
    background: rgba(13, 17, 23, 0.92);
    border-bottom: 1px solid var(--line);
    backdrop-filter: blur(8px);
    z-index: 1;
  }
  h1 {
    margin: 0;
    font-size: 1.05rem;
    font-weight: 650;
    display: flex;
    gap: 10px;
    align-items: center;
  }
  .dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--live);
    box-shadow: 0 0 0 4px rgba(63, 185, 80, 0.25);
    flex-shrink: 0;
  }
  .nav { display: flex; gap: 8px; font-size: 0.85rem; }
  .nav a {
    color: var(--muted); text-decoration: none;
    padding: 4px 10px; border-radius: 999px;
  }
  .nav a:hover { color: var(--text); }
  .nav a.active {
    color: var(--text); background: var(--card);
    border: 1px solid var(--line);
  }
  main { max-width: 920px; margin: 0 auto; padding: 20px 20px 48px; }
  .hero, .card {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 16px 18px;
    margin-bottom: 14px;
  }
  .kicker {
    margin: 0 0 12px;
    color: var(--muted);
    font-size: 0.75rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .headline {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 16px;
    align-items: center;
  }
  .team {
    display: flex; gap: 10px; align-items: center;
    font-weight: 650; min-width: 0;
  }
  .team.away { flex-direction: row-reverse; text-align: right; }
  .team span { overflow: hidden; text-overflow: ellipsis; }
  .logo {
    width: 36px; height: 36px; object-fit: contain; flex-shrink: 0;
  }
  .scoreblock { text-align: center; }
  .score {
    font-size: 2rem; font-weight: 700;
    font-variant-numeric: tabular-nums; letter-spacing: 0.04em;
  }
  .meta, .chips, .periods, .sub, .muted { color: var(--muted); }
  .clock { color: var(--live); font-weight: 650; }
  .chips, .periods {
    display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 0;
    font-size: 0.8rem;
  }
  .chips span, .periods span {
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 3px 10px;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }
  h2 {
    margin: 0 0 12px;
    font-size: 0.78rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
    font-weight: 650;
  }
  h3 { margin: 0 0 8px; font-size: 0.92rem; }
  .timeline, .xi-list, .h2h, .compare {
    list-style: none; margin: 0; padding: 0;
  }
  .timeline li, .h2h li, .compare li, .xi-list li {
    display: grid;
    grid-template-columns: 3.2rem 1fr auto;
    gap: 8px;
    padding: 6px 0;
    border-top: 1px solid var(--line);
    font-size: 0.88rem;
  }
  .xi-list li { grid-template-columns: 1.6rem 1fr auto; }
  .minute, .num, .when, .pos {
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .timeline li.away .who { color: var(--text); }
  .what em { color: var(--muted); font-style: normal; }
  .stat { margin: 10px 0 14px; }
  .stat-top, .pct, .odds, .compare li {
    display: flex; justify-content: space-between; gap: 8px;
    font-variant-numeric: tabular-nums; font-size: 0.86rem;
  }
  .stat-label {
    color: var(--muted); text-align: center; flex: 1;
  }
  .bars { display: flex; gap: 4px; height: 4px; margin-top: 6px; }
  .bars i {
    display: block; height: 4px; border-radius: 99px;
    background: var(--bar); min-width: 2px;
  }
  .bars i.away { background: var(--bar-away); margin-left: auto; }
  .xi { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  .tables { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  .tables .kicker { margin-bottom: 8px; }
  table.stand { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
  table.stand th {
    color: var(--muted); font-weight: 500; text-align: right;
    font-size: 0.72rem; padding: 0 4px 6px;
  }
  table.stand th.name, table.stand td.name { text-align: left; }
  table.stand td {
    padding: 5px 4px; border-top: 1px solid var(--line);
    text-align: right; font-variant-numeric: tabular-nums;
  }
  table.stand tr.club td.name { font-weight: 650; }
  table.stand tr.up td.name { color: var(--live); }
  table.stand tr.down td.name { color: #f85149; }
  .split, .moves { margin: 0 0 8px; font-size: 0.82rem; }
  .split { color: var(--text); }
  .delta.up { color: var(--live); }
  .delta.down { color: #f85149; }
  .delta.flat { color: var(--muted); }
  .split-title { margin-top: 16px; }
  .split-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
  .split-card {
    border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px;
    font-size: 0.8rem;
  }
  .split-card.active { border-color: var(--live); }
  .split-card p { margin: 0 0 4px; }
  .split-label {
    color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em;
    font-size: 0.68rem;
  }
  .bench { color: var(--muted); font-size: 0.8rem; margin: 10px 0 0; }
  .bench span {
    display: block; text-transform: uppercase; letter-spacing: 0.05em;
    font-size: 0.68rem; margin-bottom: 4px;
  }
  .advice { margin: 8px 0 0; }
  .odds { align-items: baseline; flex-wrap: wrap; margin: 8px 0; }
  .odds em { color: var(--muted); font-style: normal; font-weight: 500; }
  .subs { list-style: none; margin: 0; padding: 0; }
  .subs li {
    padding: 8px 0;
    border-top: 1px solid var(--line);
    font-size: 0.88rem;
  }
  .effect { margin: 4px 0 0; color: var(--muted); font-size: 0.82rem; }
  .chance {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 14px;
    align-items: baseline;
    margin: 0 0 8px;
  }
  .chance strong { font-variant-numeric: tabular-nums; font-size: 1.15rem; }
  .market { margin: 0 0 14px; }
  .market h3 {
    margin: 0 0 6px;
    font-size: 0.78rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--muted);
    font-weight: 650;
  }
  .quotes { display: flex; flex-wrap: wrap; gap: 8px; }
  .quotes span {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 4px 8px;
    font-variant-numeric: tabular-nums;
    font-size: 0.86rem;
  }
  .quotes em {
    color: var(--muted);
    font-style: normal;
    font-weight: 500;
    margin-right: 6px;
  }
  .empty { text-align: center; color: var(--muted); padding: 48px 16px; }
  .sub { font-size: 0.78rem; text-align: right; }
  @media (max-width: 720px) {
    .grid, .xi, .headline, .tables, .split-grid { grid-template-columns: 1fr; }
    .team.away { flex-direction: row; text-align: left; }
    .scoreblock { order: -1; }
  }
"""
