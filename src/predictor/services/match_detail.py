"""Read-only match page: events, stats, lineups, prediction, odds, H2H."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import aliased

from predictor.constants import FINISHED_FIXTURE_STATUSES, IN_PLAY_FIXTURE_STATUSES
from predictor.models.catalog import Player
from predictor.models.children import (
    FixtureEvent,
    FixtureLineupPlayer,
    FixtureStatistic,
)
from predictor.models.fixtures import Fixture, Team
from predictor.models.predictions import Prediction, PredictionH2H
from predictor.models.seasonal import Standing
from predictor.services.live_board import (
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
{_lineups_card(detail)}
{_prediction_card(detail)}
{_odds_card(match)}
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


def _odds_card(match: LiveMatch) -> str:
    blocks: list[str] = []
    prematch = match.prematch
    if prematch is not None:
        label = "Przed meczem" if prematch.source == "prematch" else "Otwarcie live"
        blocks.append(
            _odd_row(
                label,
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
    return f'<section class="card"><h2>Kursy i forma</h2>{"".join(blocks)}</section>'


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
    if match.form_home is not None:
        parts.append(f"{escape(match.home)} {match.form_home.avg_goals:.2f} gola")
    if match.form_away is not None:
        parts.append(f"{escape(match.away)} {match.form_away.avg_goals:.2f} gola")
    if not parts:
        return ""
    return f"<p class='muted'>Średnia goli · {' · '.join(parts)}</p>"


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
  .empty { text-align: center; color: var(--muted); padding: 48px 16px; }
  .sub { font-size: 0.78rem; text-align: right; }
  @media (max-width: 720px) {
    .grid, .xi, .headline, .tables, .split-grid { grid-template-columns: 1fr; }
    .team.away { flex-direction: row; text-align: left; }
    .scoreblock { order: -1; }
  }
"""
