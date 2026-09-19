"""Read-only live match board for the status process. Never calls API-Football."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from html import escape
from itertools import groupby
from typing import Any

from sqlalchemy import func, select, tuple_, union_all
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import aliased

from predictor.constants import FINISHED_FIXTURE_STATUSES
from predictor.models.catalog import Bookmaker, League, OddsBet, OddsLiveBet, Player
from predictor.models.children import FixtureEvent, FixtureLineup, FixtureStatistic
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture, Team, Venue
from predictor.models.odds import FixtureOdds, FixtureOddsLive
from predictor.services.ingest.next_goal import (
    is_next_goal_market,
    normalized_bet_name,
)

REFRESH_SECONDS = 15
FORM_LAST_MATCHES = 5
_GOAL_MARKET = re.compile(
    r"^which team will score the (\d+)(?:st|nd|rd|th) goal" r"( in extra time)?\??$"
)
_HT_STATUSES = frozenset({"HT", "2H", "ET", "BT", "P", "AET", "PEN", "FT"})
_ET_STATUSES = frozenset({"ET", "BT", "AET", "PEN"})
_PEN_STATUSES = frozenset({"P", "PEN"})
_EXTRA_LIVE = frozenset({"ET", "BT"})
_STAT_POSSESSION = "Ball Possession"
_STAT_SHOTS_ON = "Shots on Goal"
_STAT_SHOTS = "Total Shots"
_STAT_CORNERS = "Corner Kicks"
_STAT_REDS = "Red Cards"
_KEY_STATS = (
    _STAT_POSSESSION,
    _STAT_SHOTS_ON,
    _STAT_SHOTS,
    _STAT_CORNERS,
    _STAT_REDS,
)
_HOME_ODD_LABELS = frozenset({"1", "home"})
_AWAY_ODD_LABELS = frozenset({"2", "away"})
_DRAW_ODD_LABELS = frozenset({"x", "draw", "tie"})
_NONE_ODD_LABELS = frozenset({"no goal", "none", "no", "neither", "no goals"})
_PREMATCH_MARKETS = ("match winner", "home/away")
_LIVE_1X2_MARKETS = ("fulltime result", "full time result", "1x2")


@dataclass(frozen=True, slots=True)
class LiveScorer:
    side: str
    name: str
    minute: int
    extra: int | None
    kind: str

    @property
    def clock(self) -> str:
        if self.extra:
            return f"{self.minute}+{self.extra}'"
        return f"{self.minute}'"

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "name": self.name,
            "minute": self.minute,
            "extra": self.extra,
            "kind": self.kind,
            "clock": self.clock,
        }


@dataclass(frozen=True, slots=True)
class NextGoalOdds:
    home: str | None
    none: str | None
    away: str | None
    market: str
    suspended: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "home": self.home,
            "none": self.none,
            "away": self.away,
            "market": self.market,
            "suspended": self.suspended,
        }


@dataclass(frozen=True, slots=True)
class PrematchOdds:
    home: str | None
    draw: str | None
    away: str | None
    market: str
    bookmaker: str | None = None
    source: str = "prematch"

    def as_dict(self) -> dict[str, Any]:
        return {
            "home": self.home,
            "draw": self.draw,
            "away": self.away,
            "market": self.market,
            "bookmaker": self.bookmaker,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class TeamGoalForm:
    matches: int
    avg_goals: float
    avg_minute: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "matches": self.matches,
            "avg_goals": round(self.avg_goals, 2),
            "avg_minute": (
                None if self.avg_minute is None else round(self.avg_minute, 1)
            ),
        }

    @property
    def minute_label(self) -> str | None:
        if self.avg_minute is None:
            return None
        return f"{int(round(self.avg_minute))}'"


@dataclass(frozen=True, slots=True)
class LiveStats:
    possession_home: str | None = None
    possession_away: str | None = None
    shots_on_home: str | None = None
    shots_on_away: str | None = None
    shots_home: str | None = None
    shots_away: str | None = None
    corners_home: str | None = None
    corners_away: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "possession_home": self.possession_home,
            "possession_away": self.possession_away,
            "shots_on_home": self.shots_on_home,
            "shots_on_away": self.shots_on_away,
            "shots_home": self.shots_home,
            "shots_away": self.shots_away,
            "corners_home": self.corners_home,
            "corners_away": self.corners_away,
        }

    def is_empty(self) -> bool:
        return all(
            value is None
            for value in (
                self.possession_home,
                self.possession_away,
                self.shots_on_home,
                self.shots_on_away,
                self.shots_home,
                self.shots_away,
                self.corners_home,
                self.corners_away,
            )
        )


@dataclass(frozen=True, slots=True)
class LiveMatch:
    fixture_id: int
    country: str
    league: str
    round: str | None
    home: str
    away: str
    home_logo: str | None
    away_logo: str | None
    goals_home: int | None
    goals_away: int | None
    status_short: str
    status_long: str | None
    elapsed: int | None
    extra: int | None
    home_team_id: int = 0
    away_team_id: int = 0
    league_logo: str | None = None
    venue: str | None = None
    venue_city: str | None = None
    referee: str | None = None
    ht_home: int | None = None
    ht_away: int | None = None
    et_home: int | None = None
    et_away: int | None = None
    pen_home: int | None = None
    pen_away: int | None = None
    home_reds: int = 0
    away_reds: int = 0
    home_formation: str | None = None
    away_formation: str | None = None
    scorers: tuple[LiveScorer, ...] = ()
    stats: LiveStats | None = None
    prematch: PrematchOdds | None = None
    next_goal: NextGoalOdds | None = None
    form_home: TeamGoalForm | None = None
    form_away: TeamGoalForm | None = None

    @property
    def clock(self) -> str:
        return clock_label(self.status_short, self.elapsed, self.extra)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "country": self.country,
            "league": self.league,
            "round": self.round,
            "home": self.home,
            "away": self.away,
            "home_logo": self.home_logo,
            "away_logo": self.away_logo,
            "goals_home": self.goals_home,
            "goals_away": self.goals_away,
            "status_short": self.status_short,
            "status_long": self.status_long,
            "elapsed": self.elapsed,
            "extra": self.extra,
            "clock": self.clock,
            "home_team_id": self.home_team_id,
            "away_team_id": self.away_team_id,
            "league_logo": self.league_logo,
            "venue": self.venue,
            "venue_city": self.venue_city,
            "referee": self.referee,
            "ht_home": self.ht_home,
            "ht_away": self.ht_away,
            "et_home": self.et_home,
            "et_away": self.et_away,
            "pen_home": self.pen_home,
            "pen_away": self.pen_away,
            "home_reds": self.home_reds,
            "away_reds": self.away_reds,
            "home_formation": self.home_formation,
            "away_formation": self.away_formation,
            "scorers": [item.as_dict() for item in self.scorers],
            "stats": None if self.stats is None else self.stats.as_dict(),
            "prematch": None if self.prematch is None else self.prematch.as_dict(),
            "next_goal": (None if self.next_goal is None else self.next_goal.as_dict()),
            "form_home": None if self.form_home is None else self.form_home.as_dict(),
            "form_away": None if self.form_away is None else self.form_away.as_dict(),
        }


def clock_label(status_short: str, elapsed: int | None, extra: int | None) -> str:
    if status_short == "HT":
        return "HT"
    if elapsed is None:
        return status_short
    if extra:
        return f"{elapsed}+{extra}'"
    return f"{elapsed}'"


def safe_http_url(url: str | None) -> str | None:
    if url is None:
        return None
    stripped = url.strip()
    if stripped.startswith("https://") or stripped.startswith("http://"):
        return stripped
    return None


def parse_int_ids(raw: object) -> list[int]:
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            fid = int(item)
        except (TypeError, ValueError):
            continue
        if fid in seen:
            continue
        seen.add(fid)
        ids.append(fid)
    return ids


def parse_live_fixture_ids(params: dict[str, Any] | None) -> list[int]:
    """Ids from the last `/fixtures?live=all` task. status_short stays 2H after FT."""
    if not params:
        return []
    return parse_int_ids(params.get("fixture_ids"))


def pick_venue(
    venue_id: int | None,
    fixture_name: str | None,
    fixture_city: str | None,
    catalog_name: str | None,
    catalog_city: str | None,
) -> tuple[str | None, str | None]:
    name = _blank_to_none(fixture_name)
    city = _blank_to_none(fixture_city)
    if name is None and venue_id is not None and venue_id > 0:
        name = _blank_to_none(catalog_name)
        if city is None:
            city = _blank_to_none(catalog_city)
    elif city is None and venue_id is not None and venue_id > 0:
        city = _blank_to_none(catalog_city)
    return name, city


def event_kind(event_type: str, detail: str | None) -> str | None:
    kind = event_type.strip()
    det = (detail or "").strip().casefold()
    if kind == "Goal":
        if det == "own goal":
            return "own_goal"
        if det == "penalty":
            return "penalty"
        if det == "missed penalty":
            return None
        return "goal"
    if kind == "Card" and det in {"red card", "second yellow"}:
        return "red"
    return None


def goal_clock_minute(minute: int, extra: int | None) -> int:
    return int(minute) + int(extra or 0)


def scoring_team_id(
    event_team_id: int,
    home_id: int,
    away_id: int,
    kind: str | None,
) -> int | None:
    if kind is None:
        return None
    if kind == "own_goal":
        if event_team_id == home_id:
            return away_id
        if event_team_id == away_id:
            return home_id
        return None
    return event_team_id


def summarize_team_goal_form(
    goals_for: Sequence[int | None],
    minutes: Sequence[int],
) -> TeamGoalForm | None:
    scored = [int(value) for value in goals_for if value is not None]
    if not scored:
        return None
    avg_minute = None
    if minutes:
        avg_minute = sum(minutes) / len(minutes)
    return TeamGoalForm(
        matches=len(scored),
        avg_goals=sum(scored) / len(scored),
        avg_minute=avg_minute,
    )


def next_goal_target(
    status_short: str,
    goals_home: int | None,
    goals_away: int | None,
    et_home: int | None,
    et_away: int | None,
) -> tuple[int, bool] | None:
    if status_short in _PEN_STATUSES:
        return None
    if status_short in _EXTRA_LIVE:
        return (et_home or 0) + (et_away or 0) + 1, True
    return (goals_home or 0) + (goals_away or 0) + 1, False


def market_is_next_goal(name: str, ordinal: int, extra_time: bool) -> bool:
    if not is_next_goal_market(name):
        return False
    folded = normalized_bet_name(name)
    if folded in {"next goal", "goal next"}:
        return not extra_time
    match = _GOAL_MARKET.fullmatch(folded)
    if match is None:
        return False
    return int(match.group(1)) == ordinal and (match.group(2) is not None) == extra_time


def odd_side(label: str) -> str | None:
    folded = normalized_bet_name(label)
    if folded in _HOME_ODD_LABELS:
        return "home"
    if folded in _AWAY_ODD_LABELS:
        return "away"
    if folded in _NONE_ODD_LABELS:
        return "none"
    return None


def format_odd(odd: Decimal) -> str:
    text = format(odd, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def select_next_goal_odds(
    rows: list[tuple[str | None, str, Decimal, bool | None]],
    status_short: str,
    goals_home: int | None,
    goals_away: int | None,
    et_home: int | None,
    et_away: int | None,
) -> NextGoalOdds | None:
    target = next_goal_target(status_short, goals_home, goals_away, et_home, et_away)
    if target is None:
        return None
    ordinal, extra_time = target
    home: str | None = None
    none: str | None = None
    away: str | None = None
    market: str | None = None
    suspended = False
    for name, label, odd, flag in rows:
        if name is None or not market_is_next_goal(name, ordinal, extra_time):
            continue
        side = odd_side(label)
        if side is None:
            continue
        rendered = format_odd(odd)
        if side == "home" and home is None:
            home = rendered
        elif side == "none" and none is None:
            none = rendered
        elif side == "away" and away is None:
            away = rendered
        if market is None:
            market = name
        if flag:
            suspended = True
    if home is None and none is None and away is None:
        return None
    return NextGoalOdds(
        home=home,
        none=none,
        away=away,
        market=market or "",
        suspended=suspended,
    )


def is_match_winner_market(name: str) -> bool:
    return normalized_bet_name(name) == "match winner"


def is_home_away_market(name: str) -> bool:
    return normalized_bet_name(name) == "home/away"


def match_winner_side(label: str) -> str | None:
    folded = normalized_bet_name(label)
    if folded in _HOME_ODD_LABELS:
        return "home"
    if folded in _DRAW_ODD_LABELS:
        return "draw"
    if folded in _AWAY_ODD_LABELS:
        return "away"
    return None


def select_prematch_odds(
    rows: list[tuple[int, str | None, str | None, str, Decimal]],
) -> PrematchOdds | None:
    """One bookmaker's Match Winner 1X2; Home/Away only if 1X2 is missing."""
    winner: dict[int, dict[str, Any]] = {}
    home_away: dict[int, dict[str, Any]] = {}
    for book_id, book_name, bet_name, label, odd in rows:
        if bet_name is None:
            continue
        side = match_winner_side(label)
        if side is None:
            continue
        if is_match_winner_market(bet_name):
            bucket = winner
        elif is_home_away_market(bet_name):
            if side == "draw":
                continue
            bucket = home_away
        else:
            continue
        entry = bucket.setdefault(
            book_id, {"name": book_name, "market": bet_name, "sides": {}}
        )
        sides: dict[str, str] = entry["sides"]
        if side not in sides:
            sides[side] = format_odd(odd)
    picked = _pick_prematch_book(winner, require_draw=True)
    if picked is None:
        picked = _pick_prematch_book(home_away, require_draw=False)
    return picked


def _pick_prematch_book(
    by_book: dict[int, dict[str, Any]], *, require_draw: bool
) -> PrematchOdds | None:
    ranked: list[tuple[int, int, int, dict[str, Any]]] = []
    for book_id, entry in by_book.items():
        sides: dict[str, str] = entry["sides"]
        if len(sides) < 2:
            continue
        complete = int(
            "home" in sides
            and "away" in sides
            and (not require_draw or "draw" in sides)
        )
        ranked.append((complete, len(sides), -book_id, entry))
    if not ranked:
        return None
    _complete, _n, _bid, entry = max(ranked)
    sides = entry["sides"]
    return PrematchOdds(
        home=sides.get("home"),
        draw=sides.get("draw"),
        away=sides.get("away"),
        market=str(entry["market"]),
        bookmaker=_blank_to_none(entry["name"]),
        source="prematch",
    )


def is_fulltime_1x2_market(name: str) -> bool:
    return normalized_bet_name(name) in _LIVE_1X2_MARKETS


def select_live_1x2(
    rows: list[tuple[str | None, str, Decimal]],
) -> PrematchOdds | None:
    present = {
        normalized_bet_name(name) for name, _label, _odd in rows if name is not None
    }
    chosen: str | None = None
    for candidate in _LIVE_1X2_MARKETS:
        if candidate in present:
            chosen = candidate
            break
    if chosen is None:
        return None
    sides: dict[str, str] = {}
    market = ""
    for name, label, odd in rows:
        if name is None or normalized_bet_name(name) != chosen:
            continue
        side = match_winner_side(label)
        if side is None or side in sides:
            continue
        sides[side] = format_odd(odd)
        if not market:
            market = name
    if len(sides) < 2:
        return None
    return PrematchOdds(
        home=sides.get("home"),
        draw=sides.get("draw"),
        away=sides.get("away"),
        market=market,
        source="live",
    )


def _blank_to_none(raw: str | None) -> str | None:
    text = (raw or "").strip()
    return text if text else None


def _name(raw: str | None) -> str:
    text = (raw or "").strip()
    return text if text else "—"


def _match_noun(count: int) -> str:
    if count == 1:
        return "mecz"
    if 12 <= count % 100 <= 14:
        return "meczów"
    if 2 <= count % 10 <= 4:
        return "mecze"
    return "meczów"


async def load_live_all_fixture_ids(session: AsyncSession) -> list[int]:
    task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.fixture_id.is_(None))
        .where(EtlTask.day_utc.is_(None))
        .where(EtlTask.params.contains({"live": "all"}))
        .order_by(EtlTask.id)
        .limit(1)
    )
    if task is None:
        return []
    return parse_live_fixture_ids(task.params)


async def list_live_matches(engine: AsyncEngine) -> list[LiveMatch]:
    home = aliased(Team)
    away = aliased(Team)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        live_ids = await load_live_all_fixture_ids(session)
        if not live_ids:
            return []
        stmt = (
            select(
                Fixture.id,
                League.country_name,
                League.name,
                Fixture.round,
                home.name,
                away.name,
                home.logo,
                away.logo,
                Fixture.goals_home,
                Fixture.goals_away,
                Fixture.status_short,
                Fixture.status_long,
                Fixture.elapsed_minutes,
                Fixture.extra_minutes,
                Fixture.home_team_id,
                Fixture.away_team_id,
                League.logo,
                Fixture.venue_id,
                Fixture.venue_name,
                Fixture.venue_city,
                Venue.name,
                Venue.city,
                Fixture.referee,
                Fixture.goals_home_halftime,
                Fixture.goals_away_halftime,
                Fixture.goals_home_extratime,
                Fixture.goals_away_extratime,
                Fixture.goals_home_penalty,
                Fixture.goals_away_penalty,
            )
            .join(League, League.id == Fixture.league_id)
            .join(home, home.id == Fixture.home_team_id)
            .join(away, away.id == Fixture.away_team_id)
            .outerjoin(Venue, Venue.id == Fixture.venue_id)
            .where(Fixture.id.in_(live_ids))
            .order_by(
                League.country_name.asc().nulls_last(),
                League.name.asc().nulls_last(),
                Fixture.elapsed_minutes.desc().nulls_last(),
                Fixture.id,
            )
        )
        rows = (await session.execute(stmt)).all()
        extras = await _load_extras(session, rows)
    matches: list[LiveMatch] = []
    for row in rows:
        fixture_id = int(row[0])
        extra = extras.get(fixture_id, _empty_extras())
        venue, venue_city = pick_venue(row[17], row[18], row[19], row[20], row[21])
        status = str(row[10] or "LIVE")
        matches.append(
            LiveMatch(
                fixture_id=fixture_id,
                country=_name(row[1]),
                league=_name(row[2]),
                round=row[3],
                home=_name(row[4]),
                away=_name(row[5]),
                home_logo=safe_http_url(row[6]),
                away_logo=safe_http_url(row[7]),
                goals_home=row[8],
                goals_away=row[9],
                status_short=status,
                status_long=row[11],
                elapsed=row[12],
                extra=row[13],
                home_team_id=int(row[14]),
                away_team_id=int(row[15]),
                league_logo=safe_http_url(row[16]),
                venue=venue,
                venue_city=venue_city,
                referee=_blank_to_none(row[22]),
                ht_home=row[23],
                ht_away=row[24],
                et_home=row[25],
                et_away=row[26],
                pen_home=row[27],
                pen_away=row[28],
                home_reds=extra["home_reds"],
                away_reds=extra["away_reds"],
                home_formation=extra["home_formation"],
                away_formation=extra["away_formation"],
                scorers=extra["scorers"],
                stats=extra["stats"],
                prematch=extra["prematch"],
                next_goal=extra["next_goal"],
                form_home=extra["form_home"],
                form_away=extra["form_away"],
            )
        )
    return matches


def _empty_extras() -> dict[str, Any]:
    return {
        "home_reds": 0,
        "away_reds": 0,
        "home_formation": None,
        "away_formation": None,
        "scorers": (),
        "stats": None,
        "prematch": None,
        "next_goal": None,
        "form_home": None,
        "form_away": None,
    }


async def _load_extras(
    session: AsyncSession, rows: Sequence[Any]
) -> dict[int, dict[str, Any]]:
    if not rows:
        return {}
    ids = [int(row[0]) for row in rows]
    teams = {int(row[0]): (int(row[14]), int(row[15])) for row in rows}
    extras = {fid: _empty_extras() for fid in ids}
    await _fill_events(session, ids, teams, extras)
    await _fill_stats(session, ids, teams, extras)
    await _fill_lineups(session, ids, extras)
    await _fill_prematch(session, ids, extras)
    await _fill_next_goal(session, rows, extras)
    await _fill_goal_form(session, ids, teams, extras)
    return extras


async def _fill_events(
    session: AsyncSession,
    ids: list[int],
    teams: dict[int, tuple[int, int]],
    extras: dict[int, dict[str, Any]],
) -> None:
    stmt = (
        select(
            FixtureEvent.fixture_id,
            FixtureEvent.team_id,
            FixtureEvent.event_type,
            FixtureEvent.detail,
            FixtureEvent.minute,
            FixtureEvent.minute_extra,
            FixtureEvent.sort_order,
            Player.name,
        )
        .outerjoin(Player, Player.id == FixtureEvent.player_id)
        .where(FixtureEvent.fixture_id.in_(ids))
        .where(FixtureEvent.event_type.in_(("Goal", "Card")))
        .order_by(
            FixtureEvent.fixture_id,
            FixtureEvent.sort_order.asc().nulls_last(),
            FixtureEvent.minute,
            FixtureEvent.id,
        )
    )
    for row in (await session.execute(stmt)).all():
        fixture_id = int(row[0])
        home_id, away_id = teams[fixture_id]
        kind = event_kind(str(row[2]), row[3])
        if kind is None:
            continue
        side = _team_side(int(row[1]), home_id, away_id)
        if side is None:
            continue
        if kind == "red":
            extras[fixture_id][f"{side}_reds"] += 1
            continue
        extras[fixture_id]["scorers"] = extras[fixture_id]["scorers"] + (
            LiveScorer(
                side=side,
                name=_name(row[7]),
                minute=int(row[4]),
                extra=row[5],
                kind=kind,
            ),
        )


def _team_side(team_id: int, home_id: int, away_id: int) -> str | None:
    if team_id == home_id:
        return "home"
    if team_id == away_id:
        return "away"
    return None


def _side_appearances(
    team_ids: list[int],
    live_ids: list[int],
    team_col: Any,
    goals_col: Any,
) -> Any:
    rn = func.row_number().over(
        partition_by=team_col,
        order_by=(Fixture.date.desc().nulls_last(), Fixture.id.desc()),
    )
    inner = (
        select(
            Fixture.id.label("fixture_id"),
            team_col.label("team_id"),
            goals_col.label("goals_for"),
            Fixture.home_team_id.label("home_team_id"),
            Fixture.away_team_id.label("away_team_id"),
            Fixture.date.label("kickoff"),
            rn.label("rn"),
        )
        .where(team_col.in_(team_ids))
        .where(Fixture.status_short.in_(tuple(FINISHED_FIXTURE_STATUSES)))
    )
    if live_ids:
        inner = inner.where(Fixture.id.not_in(live_ids))
    sub = inner.subquery()
    return select(
        sub.c.fixture_id,
        sub.c.team_id,
        sub.c.goals_for,
        sub.c.home_team_id,
        sub.c.away_team_id,
        sub.c.kickoff,
    ).where(sub.c.rn <= FORM_LAST_MATCHES)


def _appearance_sort_key(row: Any) -> tuple[int, float, int]:
    kickoff = row.kickoff
    if kickoff is None:
        return (1, 0.0, -int(row.fixture_id))
    return (0, -kickoff.timestamp(), -int(row.fixture_id))


async def _fill_goal_form(
    session: AsyncSession,
    ids: list[int],
    teams: dict[int, tuple[int, int]],
    extras: dict[int, dict[str, Any]],
) -> None:
    team_ids = sorted({tid for pair in teams.values() for tid in pair})
    if not team_ids:
        return
    stmt = union_all(
        _side_appearances(team_ids, ids, Fixture.home_team_id, Fixture.goals_home),
        _side_appearances(team_ids, ids, Fixture.away_team_id, Fixture.goals_away),
    )
    appearances: dict[int, list[Any]] = defaultdict(list)
    fixture_sides: dict[int, tuple[int, int]] = {}
    for row in (await session.execute(stmt)).all():
        appearances[int(row.team_id)].append(row)
        fixture_sides[int(row.fixture_id)] = (
            int(row.home_team_id),
            int(row.away_team_id),
        )
    for rows in appearances.values():
        rows.sort(key=_appearance_sort_key)
        del rows[FORM_LAST_MATCHES:]
    wanted_by_team = {
        team_id: {int(row.fixture_id) for row in rows}
        for team_id, rows in appearances.items()
    }
    form_ids = sorted({fid for fids in wanted_by_team.values() for fid in fids})
    minutes_by_team: dict[int, list[int]] = defaultdict(list)
    if form_ids:
        events = (
            select(
                FixtureEvent.fixture_id,
                FixtureEvent.team_id,
                FixtureEvent.detail,
                FixtureEvent.minute,
                FixtureEvent.minute_extra,
            )
            .where(FixtureEvent.fixture_id.in_(form_ids))
            .where(FixtureEvent.event_type == "Goal")
        )
        for row in (await session.execute(events)).all():
            fixture_id = int(row.fixture_id)
            sides = fixture_sides.get(fixture_id)
            if sides is None:
                continue
            kind = event_kind("Goal", row.detail)
            scorer = scoring_team_id(int(row.team_id), sides[0], sides[1], kind)
            if scorer is None or fixture_id not in wanted_by_team.get(scorer, set()):
                continue
            minutes_by_team[scorer].append(
                goal_clock_minute(int(row.minute), row.minute_extra)
            )
    forms = {
        team_id: summarize_team_goal_form(
            [row.goals_for for row in rows],
            minutes_by_team.get(team_id, ()),
        )
        for team_id, rows in appearances.items()
    }
    for fixture_id, (home_id, away_id) in teams.items():
        extras[fixture_id]["form_home"] = forms.get(home_id)
        extras[fixture_id]["form_away"] = forms.get(away_id)


async def _fill_stats(
    session: AsyncSession,
    ids: list[int],
    teams: dict[int, tuple[int, int]],
    extras: dict[int, dict[str, Any]],
) -> None:
    stmt = (
        select(
            FixtureStatistic.fixture_id,
            FixtureStatistic.team_id,
            FixtureStatistic.stat_type,
            FixtureStatistic.stat_value,
        )
        .where(FixtureStatistic.fixture_id.in_(ids))
        .where(FixtureStatistic.stat_type.in_(_KEY_STATS))
        .where(FixtureStatistic.period == "FT")
    )
    buckets: dict[int, dict[str, dict[str, str | None]]] = {}
    for row in (await session.execute(stmt)).all():
        fixture_id = int(row[0])
        home_id, away_id = teams[fixture_id]
        side = _team_side(int(row[1]), home_id, away_id)
        if side is None:
            continue
        buckets.setdefault(fixture_id, {}).setdefault(str(row[2]), {})[side] = (
            _blank_to_none(row[3])
        )
    for fixture_id, stats in buckets.items():
        reds = stats.get(_STAT_REDS, {})
        if extras[fixture_id]["home_reds"] == 0:
            extras[fixture_id]["home_reds"] = _stat_count(reds.get("home"))
        if extras[fixture_id]["away_reds"] == 0:
            extras[fixture_id]["away_reds"] = _stat_count(reds.get("away"))
        live_stats = LiveStats(
            possession_home=_stat_pair(stats, _STAT_POSSESSION, "home"),
            possession_away=_stat_pair(stats, _STAT_POSSESSION, "away"),
            shots_on_home=_stat_pair(stats, _STAT_SHOTS_ON, "home"),
            shots_on_away=_stat_pair(stats, _STAT_SHOTS_ON, "away"),
            shots_home=_stat_pair(stats, _STAT_SHOTS, "home"),
            shots_away=_stat_pair(stats, _STAT_SHOTS, "away"),
            corners_home=_stat_pair(stats, _STAT_CORNERS, "home"),
            corners_away=_stat_pair(stats, _STAT_CORNERS, "away"),
        )
        extras[fixture_id]["stats"] = None if live_stats.is_empty() else live_stats


def _stat_pair(
    stats: dict[str, dict[str, str | None]], key: str, side: str
) -> str | None:
    return stats.get(key, {}).get(side)


def _stat_count(raw: str | None) -> int:
    if raw is None:
        return 0
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return 0


async def _fill_lineups(
    session: AsyncSession,
    ids: list[int],
    extras: dict[int, dict[str, Any]],
) -> None:
    stmt = select(
        FixtureLineup.fixture_id,
        FixtureLineup.is_home,
        FixtureLineup.formation,
    ).where(FixtureLineup.fixture_id.in_(ids))
    for fixture_id, is_home, formation in (await session.execute(stmt)).all():
        key = "home_formation" if is_home else "away_formation"
        extras[int(fixture_id)][key] = _blank_to_none(formation)


async def _fill_prematch(
    session: AsyncSession,
    ids: list[int],
    extras: dict[int, dict[str, Any]],
) -> None:
    stmt = (
        select(
            FixtureOdds.fixture_id,
            FixtureOdds.bookmaker_id,
            Bookmaker.name,
            OddsBet.name,
            FixtureOdds.value_label,
            FixtureOdds.odd,
        )
        .join(OddsBet, OddsBet.id == FixtureOdds.bet_id)
        .join(Bookmaker, Bookmaker.id == FixtureOdds.bookmaker_id)
        .where(FixtureOdds.fixture_id.in_(ids))
        .where(func.lower(OddsBet.name).in_(_PREMATCH_MARKETS))
    )
    by_fixture: dict[int, list[tuple[int, str | None, str | None, str, Decimal]]] = {}
    for fid, book_id, book_name, bet_name, label, odd in (
        await session.execute(stmt)
    ).all():
        by_fixture.setdefault(int(fid), []).append(
            (int(book_id), book_name, bet_name, str(label), Decimal(odd))
        )
    for fixture_id, odds_rows in by_fixture.items():
        extras[fixture_id]["prematch"] = select_prematch_odds(odds_rows)
    missing = [fid for fid in ids if extras[fid]["prematch"] is None]
    if missing:
        await _fill_live_opening_1x2(session, missing, extras)


async def _fill_live_opening_1x2(
    session: AsyncSession,
    ids: list[int],
    extras: dict[int, dict[str, Any]],
) -> None:
    bet_rows = (await session.execute(select(OddsLiveBet.id, OddsLiveBet.name))).all()
    bet_ids = [
        int(bet_id)
        for bet_id, name in bet_rows
        if name and is_fulltime_1x2_market(name)
    ]
    if not bet_ids:
        return
    earliest = (
        select(FixtureOddsLive.fixture_id, FixtureOddsLive.captured_at)
        .where(FixtureOddsLive.fixture_id.in_(ids))
        .where(FixtureOddsLive.bet_id.in_(bet_ids))
        .distinct(FixtureOddsLive.fixture_id)
        .order_by(
            FixtureOddsLive.fixture_id,
            FixtureOddsLive.captured_at.asc(),
        )
    )
    pairs = [
        (int(fid), captured)
        for fid, captured in (await session.execute(earliest)).all()
        if captured is not None
    ]
    if not pairs:
        return
    stmt = (
        select(
            FixtureOddsLive.fixture_id,
            OddsLiveBet.name,
            FixtureOddsLive.value_label,
            FixtureOddsLive.odd,
        )
        .join(OddsLiveBet, OddsLiveBet.id == FixtureOddsLive.bet_id)
        .where(
            tuple_(FixtureOddsLive.fixture_id, FixtureOddsLive.captured_at).in_(pairs)
        )
        .where(FixtureOddsLive.bet_id.in_(bet_ids))
    )
    by_fixture: dict[int, list[tuple[str | None, str, Decimal]]] = {}
    for fid, name, label, odd in (await session.execute(stmt)).all():
        by_fixture.setdefault(int(fid), []).append((name, str(label), Decimal(odd)))
    for fixture_id, odds_rows in by_fixture.items():
        extras[fixture_id]["prematch"] = select_live_1x2(odds_rows)


async def _fill_next_goal(
    session: AsyncSession,
    rows: Sequence[Any],
    extras: dict[int, dict[str, Any]],
) -> None:
    ids = [int(row[0]) for row in rows]
    mapped = await _mapped_next_goal_ids(session)
    if not mapped:
        return
    latest = (
        select(FixtureOddsLive.fixture_id, FixtureOddsLive.captured_at)
        .where(FixtureOddsLive.fixture_id.in_(ids))
        .distinct(FixtureOddsLive.fixture_id)
        .order_by(
            FixtureOddsLive.fixture_id,
            FixtureOddsLive.captured_at.desc(),
        )
    )
    pairs = [
        (int(fid), captured)
        for fid, captured in (await session.execute(latest)).all()
        if captured is not None
    ]
    if not pairs:
        return
    stmt = (
        select(
            FixtureOddsLive.fixture_id,
            OddsLiveBet.name,
            FixtureOddsLive.value_label,
            FixtureOddsLive.odd,
            FixtureOddsLive.suspended,
        )
        .join(OddsLiveBet, OddsLiveBet.id == FixtureOddsLive.bet_id)
        .where(
            tuple_(FixtureOddsLive.fixture_id, FixtureOddsLive.captured_at).in_(pairs)
        )
        .where(FixtureOddsLive.bet_id.in_(mapped))
    )
    by_fixture: dict[int, list[tuple[str | None, str, Decimal, bool | None]]] = {}
    for fid, name, label, odd, suspended in (await session.execute(stmt)).all():
        by_fixture.setdefault(int(fid), []).append(
            (name, str(label), Decimal(odd), suspended)
        )
    meta = {
        int(row[0]): (str(row[10] or "LIVE"), row[8], row[9], row[25], row[26])
        for row in rows
    }
    for fixture_id, odds_rows in by_fixture.items():
        status, goals_home, goals_away, et_home, et_away = meta[fixture_id]
        extras[fixture_id]["next_goal"] = select_next_goal_odds(
            odds_rows, status, goals_home, goals_away, et_home, et_away
        )


async def _mapped_next_goal_ids(session: AsyncSession) -> set[int]:
    task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == "/odds/live/bets")
        .order_by(EtlTask.id)
        .limit(1)
    )
    if task is None or not task.params:
        return set()
    return set(parse_int_ids(task.params.get("next_goal_bet_ids")))


def _img(url: str | None, label: str, class_name: str = "logo") -> str:
    if url is None:
        return f'<span class="{class_name} fallback" aria-hidden="true"></span>'
    src = escape(url, quote=True)
    alt = escape(label, quote=True)
    return (
        f'<img class="{class_name}" src="{src}" alt="{alt}" width="28" height="28" '
        'loading="lazy" referrerpolicy="no-referrer">'
    )


def _score(value: int | None) -> str:
    return "—" if value is None else str(value)


def _reds(count: int) -> str:
    if count <= 0:
        return ""
    shown = min(count, 5)
    cards = "".join(
        '<span class="redcard" aria-hidden="true"></span>' for _ in range(shown)
    )
    label = f"{count} czerwone" if count != 1 else "czerwona"
    return f'<span class="reds" title="{label}">{cards}</span>'


def _formation(value: str | None) -> str:
    if not value:
        return ""
    return f'<span class="formation">{escape(value)}</span>'


def _scorer_note(kind: str) -> str:
    if kind == "penalty":
        return " (k.)"
    if kind == "own_goal":
        return " (sam.)"
    return ""


def _scorer_list(match: LiveMatch, side: str) -> str:
    items = [item for item in match.scorers if item.side == side]
    if not items:
        return ""
    lines = []
    for item in items:
        name = escape(item.name)
        clock = escape(item.clock)
        note = _scorer_note(item.kind)
        lines.append(f'<li><span class="who">{name}{note}</span> {clock}</li>')
    return f'<ul class="scorer-list">{"".join(lines)}</ul>'


def _facts(match: LiveMatch) -> str:
    bits: list[str] = []
    if match.status_short in _HT_STATUSES and (
        match.ht_home is not None or match.ht_away is not None
    ):
        bits.append(f"HT {_score(match.ht_home)}–{_score(match.ht_away)}")
    if match.status_short in _ET_STATUSES and (
        match.et_home is not None or match.et_away is not None
    ):
        bits.append(f"dogr. {_score(match.et_home)}–{_score(match.et_away)}")
    if match.status_short in _PEN_STATUSES and (
        match.pen_home is not None or match.pen_away is not None
    ):
        bits.append(f"karne {_score(match.pen_home)}–{_score(match.pen_away)}")
    venue = match.venue
    if venue:
        city = match.venue_city
        bits.append(escape(f"{venue}, {city}" if city else venue))
    if match.referee:
        bits.append(f"sędzia {escape(match.referee)}")
    if match.round:
        bits.append(escape(match.round))
    if not bits:
        return ""
    return f'<p class="facts">{" · ".join(bits)}</p>'


def _stats_line(match: LiveMatch) -> str:
    stats = match.stats
    if stats is None:
        return ""
    bits: list[str] = []
    if stats.possession_home is not None or stats.possession_away is not None:
        bits.append(
            "Pos. "
            f"{escape(_score_text(stats.possession_home))}–"
            f"{escape(_score_text(stats.possession_away))}"
        )
    if stats.shots_on_home is not None or stats.shots_on_away is not None:
        bits.append(
            "Celne "
            f"{escape(_score_text(stats.shots_on_home))}–"
            f"{escape(_score_text(stats.shots_on_away))}"
        )
    if stats.shots_home is not None or stats.shots_away is not None:
        bits.append(
            "Strzały "
            f"{escape(_score_text(stats.shots_home))}–"
            f"{escape(_score_text(stats.shots_away))}"
        )
    if stats.corners_home is not None or stats.corners_away is not None:
        bits.append(
            "Rożne "
            f"{escape(_score_text(stats.corners_home))}–"
            f"{escape(_score_text(stats.corners_away))}"
        )
    if not bits:
        return ""
    return f'<p class="stats">{" · ".join(bits)}</p>'


def _form_cell(form: TeamGoalForm | None) -> str:
    if form is None:
        return "—"
    bits = [escape(f"{form.avg_goals:.1f} gola")]
    if form.minute_label is not None:
        bits.append(escape(form.minute_label))
    if form.matches != FORM_LAST_MATCHES:
        bits.append(escape(f"{form.matches} m."))
    return " · ".join(bits)


def _form_line(match: LiveMatch) -> str:
    if match.form_home is None and match.form_away is None:
        return ""
    title = escape(
        f"Średnia goli strzelonych i minuta gola z ostatnich {FORM_LAST_MATCHES} "
        "zakończonych meczów",
        quote=True,
    )
    return (
        f'<div class="form" title="{title}">'
        f'<div class="form-col home">{_form_cell(match.form_home)}</div>'
        f'<div class="form-gap">Ostatnie {FORM_LAST_MATCHES}</div>'
        f'<div class="form-col away">{_form_cell(match.form_away)}</div>'
        "</div>"
    )


def _score_text(value: str | None) -> str:
    return "—" if value is None else value


def _odds_cells(
    parts: list[tuple[str, str, str | None]],
) -> list[str]:
    cells: list[str] = []
    for class_name, label, odd in parts:
        if odd is None:
            continue
        cells.append(
            f'<span class="ng-odd {class_name}">'
            f'<span class="ng-team">{escape(label)}</span> '
            f"{escape(odd)}</span>"
        )
    return cells


def _prematch_line(match: LiveMatch) -> str:
    quote = match.prematch
    if quote is None:
        return ""
    cells = _odds_cells(
        [
            ("ng-home", match.home, quote.home),
            ("ng-draw", "Remis", quote.draw),
            ("ng-away", match.away, quote.away),
        ]
    )
    if not cells:
        return ""
    hint = " · ".join(part for part in (quote.market, quote.bookmaker) if part)
    title = f' title="{escape(hint, quote=True)}"' if hint else ""
    return (
        f'<p class="next-goal"{title}>'
        '<span class="ng-label">Przed meczem</span>'
        f"{''.join(cells)}</p>"
    )


def _next_goal_line(match: LiveMatch) -> str:
    quote = match.next_goal
    if quote is None:
        return ""
    cells = _odds_cells(
        [
            ("ng-home", match.home, quote.home),
            ("ng-none", "Brak", quote.none),
            ("ng-away", match.away, quote.away),
        ]
    )
    if not cells:
        return ""
    paused = " suspended" if quote.suspended else ""
    return (
        f'<p class="next-goal{paused}">'
        '<span class="ng-label">Następna bramka</span>'
        f"{''.join(cells)}</p>"
    )


def _match_card(match: LiveMatch) -> str:
    home = escape(match.home)
    away = escape(match.away)
    clock = escape(match.clock)
    status = escape(match.status_short)
    long_status = escape(match.status_long or match.status_short)
    home_scorers = _scorer_list(match, "home")
    away_scorers = _scorer_list(match, "away")
    scorers = ""
    if home_scorers or away_scorers:
        scorers = f"""
  <div class="scorers">
    <div class="scorer-col home">{home_scorers}</div>
    <div class="scorer-gap"></div>
    <div class="scorer-col away">{away_scorers}</div>
  </div>
"""
    return f"""
<article class="match">
  <div class="headline">
    <div class="team home">
      {_img(match.home_logo, match.home)}
      <div class="team-text">
        <span class="name">{home}{_reds(match.home_reds)}</span>
        {_formation(match.home_formation)}
      </div>
    </div>
    <div class="scoreblock">
      <div class="score">{_score(match.goals_home)}–{_score(match.goals_away)}</div>
      <div class="meta" title="{long_status}"><span class="clock">{clock}</span>
        <span class="status">{status}</span></div>
    </div>
    <div class="team away">
      {_img(match.away_logo, match.away)}
      <div class="team-text">
        <span class="name">{_reds(match.away_reds)}{away}</span>
        {_formation(match.away_formation)}
      </div>
    </div>
  </div>
    {scorers}
  {_facts(match)}
  {_stats_line(match)}
  {_form_line(match)}
  {_prematch_line(match)}
  {_next_goal_line(match)}
</article>
"""


def render_live_html(matches: list[LiveMatch], *, generated_at: datetime) -> str:
    count = len(matches)
    sections: list[str] = []
    for (country, league), rows in groupby(
        matches, key=lambda item: (item.country, item.league)
    ):
        group = list(rows)
        logo = _img(group[0].league_logo, league, class_name="league-logo")
        cards = "".join(_match_card(item) for item in group)
        sections.append(f"""
<section class="league">
  <h2>{logo}<span>{escape(country)} · {escape(league)}</span></h2>
  {cards}
</section>
""")
    body = "".join(sections)
    if not body:
        body = '<p class="empty">Żaden mecz nie jest teraz w grze.</p>'
    when = escape(generated_at.strftime("%H:%M:%S UTC"))
    noun = _match_noun(count)
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>Mecze na żywo</title>
<style>
  :root {{
    --bg: #0d1117;
    --card: #161b22;
    --line: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --live: #3fb950;
    --score: #f0f6fc;
    --red: #f85149;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.4;
  }}
  header {{
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
  }}
  h1 {{
    margin: 0;
    font-size: 1.25rem;
    font-weight: 650;
    display: flex;
    gap: 10px;
    align-items: center;
  }}
  .dot {{
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--live);
    box-shadow: 0 0 0 4px rgba(63, 185, 80, 0.25);
  }}
  .sub {{ color: var(--muted); font-size: 0.9rem; }}
  main {{ max-width: 1040px; margin: 0 auto; padding: 16px 20px 40px; }}
  .league {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    margin-bottom: 16px;
    overflow: hidden;
  }}
  h2 {{
    margin: 0;
    padding: 12px 16px;
    font-size: 0.85rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--muted);
    border-bottom: 1px solid var(--line);
    display: flex;
    gap: 8px;
    align-items: center;
  }}
  .league-logo {{
    width: 18px;
    height: 18px;
    object-fit: contain;
    flex-shrink: 0;
  }}
  .league-logo.fallback {{ display: none; }}
  .match {{
    padding: 14px 16px 12px;
    border-bottom: 1px solid var(--line);
  }}
  .match:last-child {{ border-bottom: 0; }}
  .headline {{
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 12px;
    align-items: center;
  }}
  .team {{
    display: flex;
    gap: 10px;
    align-items: center;
    min-width: 0;
  }}
  .team.away {{ flex-direction: row-reverse; text-align: right; }}
  .team-text {{ min-width: 0; }}
  .name {{
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    display: inline-flex;
    gap: 6px;
    align-items: center;
    max-width: 100%;
  }}
  .team.away .name {{ flex-direction: row-reverse; }}
  .logo {{
    width: 28px;
    height: 28px;
    object-fit: contain;
    flex-shrink: 0;
    border-radius: 4px;
    background: #0d1117;
  }}
  .logo.fallback {{
    display: inline-block;
    border: 1px solid var(--line);
  }}
  .formation {{
    display: block;
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 500;
    margin-top: 2px;
  }}
  .reds {{ display: inline-flex; gap: 2px; flex-shrink: 0; }}
  .redcard {{
    width: 7px;
    height: 10px;
    border-radius: 1px;
    background: var(--red);
  }}
  .scoreblock {{ text-align: center; min-width: 7.5rem; }}
  .score {{
    font-variant-numeric: tabular-nums;
    font-size: 1.45rem;
    font-weight: 700;
    color: var(--score);
    letter-spacing: 0.04em;
  }}
  .meta {{
    color: var(--muted);
    font-size: 0.8rem;
    display: flex;
    gap: 8px;
    justify-content: center;
  }}
  .clock {{ color: var(--live); font-weight: 650; }}
  .scorers {{
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 12px;
    margin-top: 8px;
  }}
  .scorer-list {{
    list-style: none;
    margin: 0;
    padding: 0;
    color: var(--muted);
    font-size: 0.78rem;
  }}
  .scorer-col.away {{ text-align: right; }}
  .who {{ color: var(--text); }}
  .facts, .stats, .next-goal {{
    margin: 8px 0 0;
    color: var(--muted);
    font-size: 0.78rem;
  }}
  .form {{
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 12px;
    margin-top: 8px;
    color: var(--muted);
    font-size: 0.78rem;
  }}
  .form-col.away {{ text-align: right; }}
  .form-gap {{
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.68rem;
    font-weight: 650;
    align-self: center;
  }}
  .next-goal {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px 14px;
    align-items: baseline;
  }}
  .ng-label {{
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.68rem;
    font-weight: 650;
  }}
  .ng-odd {{
    color: var(--text);
    font-variant-numeric: tabular-nums;
    font-weight: 650;
  }}
  .ng-team {{ color: var(--muted); font-weight: 500; }}
  .next-goal.suspended .ng-odd {{ color: var(--muted); }}
  .empty {{ color: var(--muted); text-align: center; padding: 48px 16px; }}
  @media (max-width: 640px) {{
    .headline, .scorers {{ grid-template-columns: 1fr; text-align: center; }}
    .team, .team.away {{ flex-direction: column; }}
    .name, .team.away .name {{ white-space: normal; flex-direction: row; }}
    .scorer-col.away {{ text-align: center; }}
    .form {{ grid-template-columns: 1fr; text-align: center; }}
    .form-col.away {{ text-align: center; }}
    .next-goal {{ justify-content: center; }}
  }}
</style>
</head>
<body>
<header>
  <h1><span class="dot" aria-hidden="true"></span> Mecze na żywo</h1>
  <p class="sub">{count} {noun} · odświeżanie co {REFRESH_SECONDS} s · {when}</p>
</header>
<main>
{body}
</main>
</body>
</html>
"""
