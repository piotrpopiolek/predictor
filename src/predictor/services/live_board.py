"""Read-only live match board for the status process. Never calls API-Football."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
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
from predictor.models.predictions import Prediction
from predictor.services.ingest.next_goal import (
    is_next_goal_market,
    normalized_bet_name,
)

REFRESH_SECONDS = 15
FORM_LAST_MATCHES = 15
_GOAL_MARKET = re.compile(
    r"^which team will score the (\d+)(?:st|nd|rd|th) goal" r"( in extra time)?\??$"
)
_HT_STATUSES = frozenset({"HT", "2H", "ET", "BT", "P", "AET", "PEN", "FT"})
_ET_STATUSES = frozenset({"ET", "BT", "AET", "PEN"})
_PEN_STATUSES = frozenset({"P", "PEN"})
_EXTRA_LIVE = frozenset({"ET", "BT"})
_SECOND_LEG_MARKERS = (
    "2nd leg",
    "second leg",
    "2nd-leg",
    "leg 2",
    "leg2",
)
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
class FirstLegScore:
    home_team_id: int
    away_team_id: int
    goals_home: int
    goals_away: int


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
    league_id: int = 0
    season: int = 0
    kickoff: datetime | None = None
    leg: int | None = None
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
    prediction_winner_team_id: int | None = None
    prediction_pct_home: float | None = None
    prediction_pct_away: float | None = None

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


def clock_sort_key(match: LiveMatch) -> tuple[int, int, int]:
    """Higher tuple sorts first (more match time elapsed)."""
    status = match.status_short
    extra = int(match.extra or 0)
    elapsed = match.elapsed
    if status in _PEN_STATUSES:
        minute = 130 + (elapsed or 0)
    elif status in {"AET", "FT"}:
        minute = 120 if status == "AET" else 90
    elif status in _EXTRA_LIVE:
        minute = 90 + (elapsed or 0) + extra
    elif status == "HT":
        minute = 45 if elapsed is None else int(elapsed)
    elif elapsed is not None:
        minute = int(elapsed) + extra
    else:
        minute = 0
    return (minute, extra, int(match.fixture_id))


def sort_matches_by_clock(matches: Sequence[LiveMatch]) -> list[LiveMatch]:
    return sorted(matches, key=clock_sort_key, reverse=True)


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


def _parse_odd(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def favorite_side(odds: PrematchOdds | None) -> str | None:
    """Side with a strictly shorter 1X2 price; draw-shortest or tied → None."""
    if odds is None:
        return None
    home = _parse_odd(odds.home)
    away = _parse_odd(odds.away)
    if home is None or away is None:
        return None
    draw = _parse_odd(odds.draw)
    if home < away and (draw is None or home < draw):
        return "home"
    if away < home and (draw is None or away < draw):
        return "away"
    return None


def _prediction_favorite(match: LiveMatch) -> str | None:
    winner = match.prediction_winner_team_id
    if winner is not None and match.home_team_id and winner == match.home_team_id:
        return "home"
    if winner is not None and match.away_team_id and winner == match.away_team_id:
        return "away"
    home_pct = match.prediction_pct_home
    away_pct = match.prediction_pct_away
    if home_pct is None or away_pct is None:
        return None
    if home_pct > away_pct:
        return "home"
    if away_pct > home_pct:
        return "away"
    return None


def match_favorite_side(match: LiveMatch) -> str | None:
    """Favorite from trusted pre-match odds, else API-Football predictions.

    Mid-match live 1X2 must not beat predictions: prices follow the score.
    Live opening lines (source=live) are only a last resort.
    """
    odds = match.prematch
    if odds is not None and odds.source == "prematch":
        side = favorite_side(odds)
        if side is not None:
            return side
    predicted = _prediction_favorite(match)
    if predicted is not None:
        return predicted
    if odds is not None and odds.source == "live":
        return favorite_side(odds)
    return None


def is_favorite_losing(match: LiveMatch) -> bool:
    if match.status_short in _PEN_STATUSES:
        return False
    side = match_favorite_side(match)
    if side is None or match.goals_home is None or match.goals_away is None:
        return False
    if side == "home":
        return match.goals_away - match.goals_home == 1
    return match.goals_home - match.goals_away == 1


def is_second_leg(round_name: str | None, leg: int | None = None) -> bool:
    if leg == 2:
        return True
    folded = (round_name or "").casefold()
    return any(marker in folded for marker in _SECOND_LEG_MARKERS)


def _goals_for_team(
    team_id: int,
    home_id: int,
    away_id: int,
    goals_home: int | None,
    goals_away: int | None,
) -> int | None:
    if goals_home is None or goals_away is None:
        return None
    if team_id == home_id:
        return int(goals_home)
    if team_id == away_id:
        return int(goals_away)
    return None


def is_tie_deficit(match: LiveMatch, first_leg: FirstLegScore | None) -> bool:
    if match.status_short in _PEN_STATUSES:
        return False
    if not is_second_leg(match.round, match.leg):
        return False
    if first_leg is None:
        return False
    side = match_favorite_side(match)
    if side is None or match.goals_home is None or match.goals_away is None:
        return False
    fav_id = match.home_team_id if side == "home" else match.away_team_id
    opp_id = match.away_team_id if side == "home" else match.home_team_id
    live_fav = match.goals_home if side == "home" else match.goals_away
    live_opp = match.goals_away if side == "home" else match.goals_home
    first_fav = _goals_for_team(
        fav_id,
        first_leg.home_team_id,
        first_leg.away_team_id,
        first_leg.goals_home,
        first_leg.goals_away,
    )
    first_opp = _goals_for_team(
        opp_id,
        first_leg.home_team_id,
        first_leg.away_team_id,
        first_leg.goals_home,
        first_leg.goals_away,
    )
    if first_fav is None or first_opp is None:
        return False
    return (int(live_opp) + first_opp) - (int(live_fav) + first_fav) == 1


def next_goal_reasons(
    match: LiveMatch, first_leg: FirstLegScore | None = None
) -> tuple[str, ...]:
    reasons: list[str] = []
    if is_favorite_losing(match):
        reasons.append("favorite_losing")
    if is_tie_deficit(match, first_leg):
        reasons.append("tie_deficit")
    return tuple(reasons)


def select_next_goal_matches(
    matches: Sequence[LiveMatch],
    first_legs: dict[int, FirstLegScore] | None = None,
) -> list[tuple[LiveMatch, tuple[str, ...]]]:
    legs = first_legs or {}
    selected: list[tuple[LiveMatch, tuple[str, ...]]] = []
    for match in matches:
        reasons = next_goal_reasons(match, legs.get(match.fixture_id))
        if reasons:
            selected.append((match, reasons))
    selected.sort(key=lambda item: clock_sort_key(item[0]), reverse=True)
    return selected


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


def _board_stmt() -> Any:
    home = aliased(Team)
    away = aliased(Team)
    return (
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
            Fixture.league_id,
            Fixture.season,
            Fixture.date,
            Fixture.leg,
        )
        .join(League, League.id == Fixture.league_id)
        .join(home, home.id == Fixture.home_team_id)
        .join(away, away.id == Fixture.away_team_id)
        .outerjoin(Venue, Venue.id == Fixture.venue_id)
    )


def _assemble_matches(
    rows: Sequence[Any], extras: dict[int, dict[str, Any]]
) -> list[LiveMatch]:
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
                league_id=int(row[29]),
                season=int(row[30]),
                kickoff=row[31],
                leg=row[32],
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
                prediction_winner_team_id=extra["prediction_winner_team_id"],
                prediction_pct_home=extra["prediction_pct_home"],
                prediction_pct_away=extra["prediction_pct_away"],
            )
        )
    return matches


def parse_query_day(raw: str | None) -> date | None:
    """Accept ``YYYY-MM-DD``. Empty input is not a day."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _kickoff_utc(match: LiveMatch) -> datetime | None:
    stamp = match.kickoff
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def order_day_matches(matches: Sequence[LiveMatch]) -> list[LiveMatch]:
    """Leagues by first kickoff, matches inside a league by kickoff."""
    far = datetime.max.replace(tzinfo=UTC)

    def kick(match: LiveMatch) -> datetime:
        stamp = _kickoff_utc(match)
        return far if stamp is None else stamp

    first: dict[tuple[str, str], datetime] = {}
    for match in matches:
        key = (match.country, match.league)
        stamp = kick(match)
        current = first.get(key)
        if current is None or stamp < current:
            first[key] = stamp
    return sorted(
        matches,
        key=lambda match: (
            first[(match.country, match.league)],
            match.country,
            match.league,
            kick(match),
            match.fixture_id,
        ),
    )


async def list_day_matches(engine: AsyncEngine, day: date) -> list[LiveMatch]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        stmt = (
            _board_stmt()
            .where(Fixture.date.is_not(None))
            .where(Fixture.date >= start)
            .where(Fixture.date < end)
            .order_by(Fixture.date.asc(), Fixture.id)
        )
        rows = (await session.execute(stmt)).all()
        extras = await _load_extras(session, rows)
    return order_day_matches(_assemble_matches(rows, extras))


async def list_live_matches(engine: AsyncEngine) -> list[LiveMatch]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        live_ids = await load_live_all_fixture_ids(session)
        if not live_ids:
            return []
        stmt = (
            _board_stmt()
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
    return sort_matches_by_clock(_assemble_matches(rows, extras))


async def load_fixture_match(engine: AsyncEngine, fixture_id: int) -> LiveMatch | None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        rows = (
            await session.execute(_board_stmt().where(Fixture.id == fixture_id))
        ).all()
        if not rows:
            return None
        extras = await _load_extras(session, rows)
    assembled = _assemble_matches(rows, extras)
    return assembled[0] if assembled else None


async def load_first_legs(
    engine: AsyncEngine, matches: Sequence[LiveMatch]
) -> dict[int, FirstLegScore]:
    candidates = [
        match
        for match in matches
        if is_second_leg(match.round, match.leg)
        and match.home_team_id
        and match.away_team_id
        and match.league_id
    ]
    if not candidates:
        return {}
    live_ids = [match.fixture_id for match in candidates]
    team_pairs = {
        (min(m.home_team_id, m.away_team_id), max(m.home_team_id, m.away_team_id))
        for m in candidates
    }
    league_ids = sorted({m.league_id for m in candidates})
    pair_filter = tuple_(
        func.least(Fixture.home_team_id, Fixture.away_team_id),
        func.greatest(Fixture.home_team_id, Fixture.away_team_id),
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        stmt = (
            select(
                Fixture.id,
                Fixture.league_id,
                Fixture.home_team_id,
                Fixture.away_team_id,
                Fixture.goals_home,
                Fixture.goals_away,
                Fixture.goals_home_fulltime,
                Fixture.goals_away_fulltime,
                Fixture.date,
            )
            .where(Fixture.league_id.in_(league_ids))
            .where(Fixture.status_short.in_(tuple(FINISHED_FIXTURE_STATUSES)))
            .where(Fixture.id.not_in(live_ids))
            .where(pair_filter.in_(list(team_pairs)))
            .order_by(Fixture.date.desc().nulls_last(), Fixture.id.desc())
        )
        rows = (await session.execute(stmt)).all()

    def _score(row: Any) -> FirstLegScore | None:
        goals_home = row[6] if row[6] is not None else row[4]
        goals_away = row[7] if row[7] is not None else row[5]
        if goals_home is None or goals_away is None:
            return None
        return FirstLegScore(
            home_team_id=int(row[2]),
            away_team_id=int(row[3]),
            goals_home=int(goals_home),
            goals_away=int(goals_away),
        )

    result: dict[int, FirstLegScore] = {}
    for match in candidates:
        for row in rows:
            if int(row[1]) != match.league_id:
                continue
            if {int(row[2]), int(row[3])} != {
                match.home_team_id,
                match.away_team_id,
            }:
                continue
            kickoff = row[8]
            if (
                match.kickoff is not None
                and kickoff is not None
                and kickoff >= match.kickoff
            ):
                continue
            scored = _score(row)
            if scored is None:
                continue
            result[match.fixture_id] = scored
            break
    return result


async def list_next_goal_matches(
    engine: AsyncEngine,
) -> list[tuple[LiveMatch, tuple[str, ...]]]:
    matches = await list_live_matches(engine)
    first_legs = await load_first_legs(engine, matches)
    return select_next_goal_matches(matches, first_legs)


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
        "prediction_winner_team_id": None,
        "prediction_pct_home": None,
        "prediction_pct_away": None,
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
    await _fill_predictions(session, ids, extras)
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
    """Attach earliest live 1X2 only when captured at or before kickoff."""
    bet_rows = (await session.execute(select(OddsLiveBet.id, OddsLiveBet.name))).all()
    bet_ids = [
        int(bet_id)
        for bet_id, name in bet_rows
        if name and is_fulltime_1x2_market(name)
    ]
    if not bet_ids:
        return
    kickoffs = {
        int(fid): kickoff
        for fid, kickoff in (
            await session.execute(
                select(Fixture.id, Fixture.date).where(Fixture.id.in_(ids))
            )
        ).all()
    }
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
    pairs = []
    for fid, captured in (await session.execute(earliest)).all():
        if captured is None:
            continue
        kickoff = kickoffs.get(int(fid))
        if kickoff is not None and captured > kickoff:
            continue
        pairs.append((int(fid), captured))
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


def _parse_pct(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().rstrip("%")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


async def _fill_predictions(
    session: AsyncSession,
    ids: list[int],
    extras: dict[int, dict[str, Any]],
) -> None:
    stmt = select(
        Prediction.fixture_id,
        Prediction.winner_team_id,
        Prediction.pct_home,
        Prediction.pct_away,
    ).where(Prediction.fixture_id.in_(ids))
    rows = (await session.execute(stmt)).all()
    for fixture_id, winner_id, pct_home, pct_away in rows:
        extras[int(fixture_id)]["prediction_winner_team_id"] = (
            None if winner_id is None else int(winner_id)
        )
        extras[int(fixture_id)]["prediction_pct_home"] = _parse_pct(pct_home)
        extras[int(fixture_id)]["prediction_pct_away"] = _parse_pct(pct_away)


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
    label = "Przed meczem" if quote.source == "prematch" else "Otwarcie live"
    return (
        f'<p class="next-goal"{title}>'
        f'<span class="ng-label">{label}</span>'
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


_LIVE_CSS = """
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --line: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --live: #3fb950;
    --score: #f0f6fc;
    --red: #f85149;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.4;
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
    font-size: 1.25rem;
    font-weight: 650;
    display: flex;
    gap: 10px;
    align-items: center;
  }
  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--live);
    box-shadow: 0 0 0 4px rgba(63, 185, 80, 0.25);
  }
  .header-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 18px;
    align-items: baseline;
  }
  .nav {
    display: flex;
    gap: 8px;
    font-size: 0.85rem;
  }
  .nav a {
    color: var(--muted);
    text-decoration: none;
    padding: 4px 10px;
    border-radius: 999px;
    border: 1px solid transparent;
  }
  .nav a:hover { color: var(--text); }
  .nav a.active {
    color: var(--text);
    border-color: var(--line);
    background: var(--card);
  }
  .sub { color: var(--muted); font-size: 0.9rem; }
  main { max-width: 1040px; margin: 0 auto; padding: 16px 20px 40px; }
  .league {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    margin-bottom: 16px;
    overflow: hidden;
  }
  h2 {
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
  }
  .league-logo {
    width: 18px;
    height: 18px;
    object-fit: contain;
    flex-shrink: 0;
  }
  .league-logo.fallback { display: none; }
  .match {
    padding: 14px 16px 12px;
    border-bottom: 1px solid var(--line);
  }
  .match:last-child { border-bottom: 0; }
  .match-link {
    display: block;
    color: inherit;
    text-decoration: none;
  }
  .match-link:hover { background: rgba(255, 255, 255, 0.03); }
  .headline {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 12px;
    align-items: center;
  }
  .team {
    display: flex;
    gap: 10px;
    align-items: center;
    min-width: 0;
  }
  .team.away { flex-direction: row-reverse; text-align: right; }
  .team-text { min-width: 0; }
  .name {
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    display: inline-flex;
    gap: 6px;
    align-items: center;
    max-width: 100%;
  }
  .team.away .name { flex-direction: row-reverse; }
  .logo {
    width: 28px;
    height: 28px;
    object-fit: contain;
    flex-shrink: 0;
    border-radius: 4px;
    background: #0d1117;
  }
  .logo.fallback {
    display: inline-block;
    border: 1px solid var(--line);
  }
  .formation {
    display: block;
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 500;
    margin-top: 2px;
  }
  .reds { display: inline-flex; gap: 2px; flex-shrink: 0; }
  .redcard {
    width: 7px;
    height: 10px;
    border-radius: 1px;
    background: var(--red);
  }
  .scoreblock { text-align: center; min-width: 7.5rem; }
  .score {
    font-variant-numeric: tabular-nums;
    font-size: 1.45rem;
    font-weight: 700;
    color: var(--score);
    letter-spacing: 0.04em;
  }
  .meta {
    color: var(--muted);
    font-size: 0.8rem;
    display: flex;
    gap: 8px;
    justify-content: center;
  }
  .clock { color: var(--live); font-weight: 650; }
  .scorers {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 12px;
    margin-top: 8px;
  }
  .scorer-list {
    list-style: none;
    margin: 0;
    padding: 0;
    color: var(--muted);
    font-size: 0.78rem;
  }
  .scorer-col.away { text-align: right; }
  .who { color: var(--text); }
  .facts, .stats, .next-goal {
    margin: 8px 0 0;
    color: var(--muted);
    font-size: 0.78rem;
  }
  .form {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 12px;
    margin-top: 8px;
    color: var(--muted);
    font-size: 0.78rem;
  }
  .form-col.away { text-align: right; }
  .form-gap {
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.68rem;
    font-weight: 650;
    align-self: center;
  }
  .next-goal {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 14px;
    align-items: baseline;
  }
  .ng-label {
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.68rem;
    font-weight: 650;
  }
  .ng-odd {
    color: var(--text);
    font-variant-numeric: tabular-nums;
    font-weight: 650;
  }
  .ng-team { color: var(--muted); font-weight: 500; }
  .next-goal.suspended .ng-odd { color: var(--muted); }
  .empty { color: var(--muted); text-align: center; padding: 48px 16px; }
  @media (max-width: 640px) {
    .headline, .scorers { grid-template-columns: 1fr; text-align: center; }
    .team, .team.away { flex-direction: column; }
    .name, .team.away .name { white-space: normal; flex-direction: row; }
    .scorer-col.away { text-align: center; }
    .form { grid-template-columns: 1fr; text-align: center; }
    .form-col.away { text-align: center; }
    .next-goal { justify-content: center; }
  }

  .bet-actions, .bet-open {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px dashed var(--line);
  }
  .bet-toggle, .bet-form button, .bet-settle button {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 6px 12px;
    font: inherit;
    cursor: pointer;
  }
  .bet-toggle:hover, .bet-form button:hover, .bet-settle button:hover {
    border-color: var(--muted);
  }
  .bet-form {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 14px;
    align-items: center;
    margin-top: 8px;
  }
  .bet-market {
    margin: 0;
    width: 100%;
    color: var(--muted);
    font-size: 0.85rem;
  }
  .bet-market strong { color: var(--text); }
  .bet-odd input {
    width: 5.5rem;
    margin-left: 6px;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 4px 8px;
    font: inherit;
  }
  .bet-stake { color: var(--muted); font-size: 0.82rem; }
  .bet-badge {
    display: inline-block;
    color: var(--live);
    font-size: 0.82rem;
    font-weight: 650;
    margin-right: 10px;
  }
  .bet-settle { display: inline-flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
  .day-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    margin: 0 0 18px;
  }
  .day-bar a, .day-bar button {
    color: var(--text);
    text-decoration: none;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 6px 12px;
    font: inherit;
    cursor: pointer;
  }
  .day-bar a:hover, .day-bar button:hover { border-color: var(--muted); }
  .day-bar input[type="date"] {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 6px 8px;
    font: inherit;
  }
  .day-error { color: #f85149; margin: 0 0 12px; }
  .kickoff {
    color: var(--muted);
    margin-right: 6px;
    font-variant-numeric: tabular-nums;
  }
"""


def _match_card(
    match: LiveMatch,
    *,
    allow_bet: bool = False,
    open_bet: dict[str, Any] | None = None,
    current_stake: str | None = None,
    show_kickoff: bool = False,
) -> str:
    home = escape(match.home)
    away = escape(match.away)
    clock = escape(match.clock)
    status = escape(match.status_short)
    long_status = escape(match.status_long or match.status_short)
    kickoff_html = ""
    if show_kickoff:
        kickoff = _kickoff_utc(match)
        if kickoff is not None:
            kickoff_html = (
                f'<span class="kickoff">{escape(kickoff.strftime("%H:%M"))}</span>'
            )
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
    bet_block = ""
    if allow_bet:
        bet_block = _bet_block(match, open_bet=open_bet, current_stake=current_stake)
    return f"""
<article class="match" data-fixture-id="{match.fixture_id}">
  <a class="match-link" href="/live/match/{match.fixture_id}">
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
      <div class="meta" title="{long_status}">
        {kickoff_html}<span class="clock">{clock}</span>
        <span class="status">{status}</span>
      </div>
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
  </a>
  {bet_block}
</article>
"""


def _bet_block(
    match: LiveMatch,
    *,
    open_bet: dict[str, Any] | None,
    current_stake: str | None,
) -> str:
    if open_bet is not None:
        odd = escape(str(open_bet.get("odd", "")))
        stake = escape(str(open_bet.get("stake", "")))
        bet_id = escape(str(open_bet.get("id", "")))
        return f"""
  <div class="bet-open" data-open-bet="1">
    <span class="bet-badge">Otwarty · Gol @ {odd} · stawka {stake}</span>
    <form class="bet-settle" method="post" action="/live/bets/{bet_id}/settle">
      <button type="submit" name="outcome" value="won">Wygrana</button>
      <button type="submit" name="outcome" value="lost">Przegrana</button>
      <button type="submit" name="outcome" value="void">Void</button>
    </form>
  </div>
"""
    stake_label = escape(current_stake or "21.00")
    return f"""
  <div class="bet-actions">
    <button type="button" class="bet-toggle" aria-expanded="false">
      Zagraj następny gol
    </button>
    <form class="bet-form" method="post" action="/live/bets" hidden>
      <input type="hidden" name="fixture_id" value="{match.fixture_id}">
      <p class="bet-market">
        Następny gol · <strong>padnie</strong> (bez względu kto strzeli)
      </p>
      <label class="bet-odd">Kurs
        <input type="text" name="odd" inputmode="decimal" placeholder="1.65" required
          pattern="[0-9]+([.,][0-9]+)?" autocomplete="off">
      </label>
      <span class="bet-stake">Stawka {stake_label}</span>
      <div class="bet-submit">
        <button type="submit">Zapisz zakład</button>
        <button type="button" class="bet-cancel">Anuluj</button>
      </div>
    </form>
  </div>
"""


_REFRESH_SCRIPT = f"""
<script>
(function () {{
  var SECONDS = {REFRESH_SECONDS};
  function formOpen() {{
    var forms = document.querySelectorAll(".bet-form");
    for (var i = 0; i < forms.length; i++) {{
      if (!forms[i].hidden) return true;
    }}
    var ae = document.activeElement;
    if (ae && ae.matches("input, textarea, select, button")) {{
      return ae.closest(".bet-form, .bet-settle") != null;
    }}
    return false;
  }}
  function schedule() {{
    setTimeout(function () {{
      if (formOpen()) {{
        schedule();
        return;
      }}
      location.reload();
    }}, SECONDS * 1000);
  }}
  document.addEventListener("click", function (ev) {{
    var toggle = ev.target.closest(".bet-toggle");
    if (toggle) {{
      var wrap = toggle.closest(".bet-actions");
      if (!wrap) return;
      var form = wrap.querySelector(".bet-form");
      if (!form) return;
      form.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
      toggle.hidden = true;
      var odd = form.querySelector('input[name="odd"]');
      if (odd) odd.focus();
      return;
    }}
    var cancel = ev.target.closest(".bet-cancel");
    if (cancel) {{
      var wrap = cancel.closest(".bet-actions");
      if (!wrap) return;
      var form = wrap.querySelector(".bet-form");
      var toggleBtn = wrap.querySelector(".bet-toggle");
      if (form) form.hidden = true;
      if (toggleBtn) {{
        toggleBtn.hidden = false;
        toggleBtn.setAttribute("aria-expanded", "false");
      }}
    }}
  }});
  schedule();
}})();
</script>
"""


def render_live_html(
    matches: list[LiveMatch],
    *,
    generated_at: datetime,
    title: str = "Mecze na żywo",
    empty: str = "Żaden mecz nie jest teraz w grze.",
    active_nav: str = "all",
    open_bets: dict[int, dict[str, Any]] | None = None,
    current_stake: str | None = None,
) -> str:
    ordered = sort_matches_by_clock(matches)
    count = len(ordered)
    allow_bet = active_nav == "next_goal"
    bets = open_bets or {}
    sections: list[str] = []
    for (country, league), rows in groupby(
        ordered, key=lambda item: (item.country, item.league)
    ):
        group = list(rows)
        logo = _img(group[0].league_logo, league, class_name="league-logo")
        cards = "".join(
            _match_card(
                item,
                allow_bet=allow_bet,
                open_bet=bets.get(item.fixture_id),
                current_stake=current_stake,
            )
            for item in group
        )
        sections.append(f"""
<section class="league">
  <h2>{logo}<span>{escape(country)} · {escape(league)}</span></h2>
  {cards}
</section>
""")
    body = "".join(sections)
    if not body:
        body = f'<p class="empty">{escape(empty)}</p>'
    when = escape(generated_at.strftime("%H:%M:%S UTC"))
    noun = _match_noun(count)
    nav = _live_nav(active_nav)
    page_title = escape(title)
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
<style>
{_LIVE_CSS}
</style>
</head>
<body>
<header>
  <h1><span class="dot" aria-hidden="true"></span> {page_title}</h1>
  <div class="header-meta">
    {nav}
    <p class="sub">{count} {noun} · odświeżanie co {REFRESH_SECONDS} s · {when}</p>
  </div>
</header>
<main>
  {body}
</main>
{_REFRESH_SCRIPT}
</body>
</html>
"""


_WEEKDAYS_PL = (
    "poniedziałek",
    "wtorek",
    "środa",
    "czwartek",
    "piątek",
    "sobota",
    "niedziela",
)


def format_day_heading(day: date) -> str:
    weekday = _WEEKDAYS_PL[day.weekday()]
    return f"{weekday} {day.strftime('%d.%m.%Y')}"


def render_day_html(
    matches: list[LiveMatch],
    *,
    day: date,
    today: date,
    generated_at: datetime,
    invalid_date: bool = False,
) -> str:
    ordered = order_day_matches(matches)
    sections: list[str] = []
    for (country, league), rows in groupby(
        ordered, key=lambda item: (item.country, item.league)
    ):
        group = list(rows)
        logo = _img(group[0].league_logo, league, class_name="league-logo")
        cards = "".join(_match_card(item, show_kickoff=True) for item in group)
        sections.append(f"""
<section class="league">
  <h2>{logo}<span>{escape(country)} · {escape(league)}</span></h2>
  {cards}
</section>
""")
    body = "".join(sections)
    if not body:
        body = '<p class="empty">Brak meczów w tym dniu.</p>'
    previous = day - timedelta(days=1)
    following = day + timedelta(days=1)
    today_link = ""
    if day != today:
        today_link = '<a href="/live/day">Dziś</a>'
    error = ""
    if invalid_date:
        error = '<p class="day-error">Nie rozpoznaję tej daty. Pokazuję dziś.</p>'
    heading = escape(format_day_heading(day))
    when = escape(generated_at.strftime("%H:%M:%S UTC"))
    count = len(ordered)
    noun = _match_noun(count)
    refresh = ""
    refresh_note = "godziny UTC"
    if day == today and not invalid_date:
        refresh = _REFRESH_SCRIPT
        refresh_note = f"odświeżanie co {REFRESH_SECONDS} s · godziny UTC"
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mecze · {heading}</title>
<style>
{_LIVE_CSS}
</style>
</head>
<body>
<header>
  <h1>Mecze · {heading}</h1>
  <div class="header-meta">
    {_live_nav("day")}
    <p class="sub">{count} {noun} · {refresh_note} · {when}</p>
  </div>
</header>
<main>
  {error}
  <form class="day-bar" method="get" action="/live/day">
    <a href="/live/day/{previous.isoformat()}">← {previous.strftime("%d.%m")}</a>
    <input type="date" name="day" value="{day.isoformat()}" required>
    <button type="submit">Pokaż</button>
    {today_link}
    <a href="/live/day/{following.isoformat()}">{following.strftime("%d.%m")} →</a>
  </form>
  {body}
</main>
{refresh}
</body>
</html>
"""


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}%".replace(".", ",")


def _pl_num(raw: str | float | Decimal | None) -> str:
    if raw is None:
        return "—"
    text = format(Decimal(str(raw)), "f")
    if "." in text:
        whole, frac = text.split(".", 1)
        return f"{whole},{frac}"
    return text


_BETS_CSS = """
  .metrics {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 12px;
    margin-bottom: 20px;
  }
  .metrics div {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 12px 14px;
  }
  .metrics strong {
    display: block;
    font-size: 1.15rem;
    font-variant-numeric: tabular-nums;
  }
  .metrics span {
    color: var(--muted);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .table-wrap {
    overflow-x: auto;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--card);
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
  }
  th, td {
    padding: 8px 10px;
    border-bottom: 1px solid var(--line);
    text-align: left;
    white-space: nowrap;
  }
  th {
    color: var(--muted);
    font-weight: 600;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr.won td:nth-child(7) { color: var(--live); }
  tr.lost td:nth-child(7) { color: var(--red); }
  tr.void td:nth-child(7) { color: var(--muted); }
  tr.open td:nth-child(7) { color: #58a6ff; }
  .empty-row { text-align: center; color: var(--muted); padding: 24px !important; }
  .open-block {
    margin-bottom: 16px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 12px 16px;
  }
  .open-block h2 {
    border: 0;
    padding: 0 0 8px;
    text-transform: none;
    letter-spacing: 0;
    font-size: 0.95rem;
    color: var(--text);
  }
  .open-block ul { margin: 0; padding-left: 18px; }
  .open-block li {
    margin: 6px 0;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .bet-settle.inline { display: inline-flex; gap: 4px; }
  .charts {
    display: grid;
    grid-template-columns: 1fr;
    gap: 16px;
    margin-top: 20px;
  }
  @media (min-width: 960px) {
    .charts { grid-template-columns: 1fr 1fr; }
  }
  .chart-card {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 12px 14px 8px;
  }
  .chart-card h2 {
    margin: 0 0 8px;
    font-size: 0.95rem;
    font-weight: 600;
    color: var(--text);
  }
  .chart-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 14px;
    margin: 0 0 8px;
    font-size: 0.72rem;
    color: var(--muted);
  }
  .chart-legend span {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  .chart-legend i {
    width: 12px;
    height: 3px;
    border-radius: 1px;
    display: inline-block;
  }
  .chart-empty {
    color: var(--muted);
    font-size: 0.85rem;
    padding: 24px 8px;
    text-align: center;
  }
  .chart-card svg {
    width: 100%;
    height: auto;
    display: block;
  }
"""


def _fnum(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _svg_polyline(
    xs: list[float],
    ys: list[float],
    *,
    color: str,
    width: float = 1.6,
    opacity: float = 1.0,
) -> str:
    if len(xs) < 2 or len(xs) != len(ys):
        return ""
    points = " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, ys, strict=True))
    return (
        f'<polyline fill="none" stroke="{color}" stroke-width="{width}" '
        f'stroke-linejoin="round" stroke-linecap="round" '
        f'opacity="{opacity}" points="{points}"/>'
    )


def _moving_average(values: list[float], window: int) -> list[float]:
    if not values:
        return []
    w = max(1, min(window, len(values)))
    out: list[float] = []
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= w:
            running -= values[i - w]
            out.append(running / w)
        else:
            out.append(running / (i + 1))
    return out


def _chart_layout(
    series: list[list[float]],
    *,
    width: int = 640,
    height: int = 260,
    pad_l: float = 44,
    pad_r: float = 12,
    pad_t: float = 12,
    pad_b: float = 28,
) -> tuple[list[list[tuple[float, float]]], float, float, float, float]:
    flat = [v for row in series for v in row]
    if not flat:
        return [], 0.0, 1.0, pad_l, pad_t
    y_min = min(flat)
    y_max = max(flat)
    if abs(y_max - y_min) < 1e-9:
        y_min -= 1.0
        y_max += 1.0
    # pad 8%
    span = y_max - y_min
    y_min -= span * 0.08
    y_max += span * 0.08
    n = max(len(row) for row in series)
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def x_at(i: int) -> float:
        if n <= 1:
            return pad_l + plot_w / 2
        return pad_l + plot_w * (i / (n - 1))

    def y_at(v: float) -> float:
        return pad_t + plot_h * (1.0 - (v - y_min) / (y_max - y_min))

    plotted: list[list[tuple[float, float]]] = []
    for row in series:
        plotted.append([(x_at(i), y_at(v)) for i, v in enumerate(row)])
    return plotted, y_min, y_max, pad_l, pad_t


def _y_grid_svg(
    y_min: float,
    y_max: float,
    *,
    width: int,
    height: int,
    pad_l: float,
    pad_r: float,
    pad_t: float,
    pad_b: float,
    ticks: int = 5,
    as_pct: bool = False,
) -> str:
    parts: list[str] = []
    plot_h = height - pad_t - pad_b
    plot_w = width - pad_l - pad_r
    for i in range(ticks):
        t = i / (ticks - 1) if ticks > 1 else 0.0
        value = y_max - t * (y_max - y_min)
        y = pad_t + plot_h * t
        label = f"{value:.0f}%" if as_pct else f"{value:.0f}"
        parts.append(
            f'<line x1="{pad_l:.1f}" y1="{y:.1f}" x2="{pad_l + plot_w:.1f}" '
            f'y2="{y:.1f}" stroke="#30363d" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 6:.1f}" y="{y + 3:.1f}" text-anchor="end" '
            f'fill="#8b949e" font-size="10">{label}</text>'
        )
    # zero line when in range
    if y_min < 0 < y_max and not as_pct:
        y0 = pad_t + plot_h * (1.0 - (0 - y_min) / (y_max - y_min))
        parts.append(
            f'<line x1="{pad_l:.1f}" y1="{y0:.1f}" x2="{pad_l + plot_w:.1f}" '
            f'y2="{y0:.1f}" stroke="#484f58" stroke-width="1.2"/>'
        )
    return "".join(parts)


def _bets_chart_series(bets: list[dict[str, Any]]) -> list[dict[str, float]]:
    """Chronological points for charts (skip open for pnl/hit progress)."""
    ordered = sorted(
        bets,
        key=lambda row: (str(row.get("placed_at") or ""), int(row.get("id") or 0)),
    )
    points: list[dict[str, float]] = []
    for bet in ordered:
        status = str(bet.get("status") or "")
        stake = _fnum(bet.get("stake"))
        if stake is None:
            continue
        pnl = _fnum(bet.get("pnl")) or 0.0
        saldo = _fnum(bet.get("saldo"))
        hit = _fnum(bet.get("hit_overall"))
        if status == "open":
            # Keep stake path; saldo/hit stay at last known via forward fill below.
            points.append(
                {
                    "stake": stake,
                    "pnl": 0.0,
                    "saldo": points[-1]["saldo"] if points else 0.0,
                    "hit": points[-1]["hit"] if points else 0.0,
                    "decided": 0.0,
                }
            )
            continue
        points.append(
            {
                "stake": stake,
                "pnl": pnl,
                "saldo": 0.0 if saldo is None else saldo,
                "hit": 0.0 if hit is None else hit,
                "decided": 1.0 if status in {"won", "lost"} else 0.0,
            }
        )
    return points


def render_bets_saldo_chart(bets: list[dict[str, Any]]) -> str:
    points = _bets_chart_series(bets)
    if len(points) < 2:
        return (
            '<section class="chart-card"><h2>Saldo</h2>'
            '<p class="chart-empty">Za mało typów na wykres (min. 2).</p></section>'
        )
    saldo = [p["saldo"] for p in points]
    stake = [p["stake"] for p in points]
    pnl = [p["pnl"] for p in points]
    stake_avg_val = sum(stake) / len(stake)
    stake_avg = [stake_avg_val] * len(stake)
    trend = _moving_average(saldo, window=min(10, len(saldo)))

    width, height = 640, 280
    pad_l, pad_r, pad_t, pad_b = 48.0, 14.0, 14.0, 30.0
    plotted, y_min, y_max, _, _ = _chart_layout(
        [saldo, trend, stake, stake_avg, pnl],
        width=width,
        height=height,
        pad_l=pad_l,
        pad_r=pad_r,
        pad_t=pad_t,
        pad_b=pad_b,
    )
    grid = _y_grid_svg(
        y_min,
        y_max,
        width=width,
        height=height,
        pad_l=pad_l,
        pad_r=pad_r,
        pad_t=pad_t,
        pad_b=pad_b,
    )
    colors = ("#58a6ff", "#79c0ff", "#f85149", "#d4a72c", "#3fb950")
    widths = (2.2, 1.6, 1.5, 1.3, 1.2)
    opacities = (1.0, 0.75, 1.0, 0.9, 0.85)
    lines = []
    for pts, color, w, op in zip(plotted, colors, widths, opacities, strict=True):
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        lines.append(_svg_polyline(xs, ys, color=color, width=w, opacity=op))
    legend = (
        '<div class="chart-legend">'
        '<span><i style="background:#58a6ff"></i>Saldo</span>'
        '<span><i style="background:#79c0ff"></i>Trend</span>'
        '<span><i style="background:#f85149"></i>Stawka</span>'
        '<span><i style="background:#d4a72c"></i>Śr. stawka</span>'
        '<span><i style="background:#3fb950"></i>Średnia zmiana</span>'
        "</div>"
    )
    return (
        f'<section class="chart-card"><h2>Saldo</h2>{legend}'
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Wykres salda, stawki i zmiany">'
        f"{grid}{''.join(lines)}</svg></section>"
    )


def render_bets_hit_chart(bets: list[dict[str, Any]]) -> str:
    points = [p for p in _bets_chart_series(bets) if p["decided"] > 0]
    if len(points) < 2:
        return (
            '<section class="chart-card"><h2>Skuteczność</h2>'
            '<p class="chart-empty">Za mało rozliczonych typów na wykres (min. 2).</p>'
            "</section>"
        )
    hit = [p["hit"] for p in points]
    hit_avg_val = hit[-1]
    hit_avg = [hit_avg_val] * len(hit)
    width, height = 640, 280
    pad_l, pad_r, pad_t, pad_b = 48.0, 14.0, 14.0, 30.0
    # Force 0–100 scale with padding
    series_for_scale = [hit, hit_avg, [0.0], [100.0]]
    plotted, y_min, y_max, _, _ = _chart_layout(
        series_for_scale,
        width=width,
        height=height,
        pad_l=pad_l,
        pad_r=pad_r,
        pad_t=pad_t,
        pad_b=pad_b,
    )
    grid = _y_grid_svg(
        y_min,
        y_max,
        width=width,
        height=height,
        pad_l=pad_l,
        pad_r=pad_r,
        pad_t=pad_t,
        pad_b=pad_b,
        as_pct=True,
    )
    hit_line = _svg_polyline(
        [p[0] for p in plotted[0]],
        [p[1] for p in plotted[0]],
        color="#a371f7",
        width=2.2,
    )
    avg_line = _svg_polyline(
        [p[0] for p in plotted[1]],
        [p[1] for p in plotted[1]],
        color="#d2a8ff",
        width=1.4,
        opacity=0.8,
    )
    legend = (
        '<div class="chart-legend">'
        '<span><i style="background:#a371f7"></i>Hit (narastająco)</span>'
        '<span><i style="background:#d2a8ff"></i>Bieżąca skuteczność</span>'
        "</div>"
    )
    return (
        f'<section class="chart-card"><h2>Skuteczność</h2>{legend}'
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Wykres skuteczności">'
        f"{grid}{hit_line}{avg_line}</svg></section>"
    )


def render_bets_html(
    payload: dict[str, Any],
    *,
    generated_at: datetime,
) -> str:
    metrics = payload.get("metrics") or {}
    bets = list(payload.get("bets") or [])
    when = escape(generated_at.strftime("%H:%M:%S UTC"))
    nav = _live_nav("bets")
    hit = _pct(metrics.get("hit_rate"))
    rows_html: list[str] = []
    for bet in reversed(bets):
        status = str(bet.get("status", ""))
        tone = {
            "won": "won",
            "lost": "lost",
            "void": "void",
            "open": "open",
        }.get(status, "")
        match = (
            f"{escape(str(bet.get('home', '')))} – "
            f"{escape(str(bet.get('away', '')))}"
        )
        placed = escape(str(bet.get("placed_at", ""))[:16].replace("T", " "))
        rows_html.append(
            f"<tr class='{tone}'>"
            f"<td>{escape(str(bet.get('id', '')))}</td>"
            f"<td>{placed}</td>"
            f"<td>{escape(str(bet.get('league', '')))}</td>"
            f"<td>{match}</td>"
            f"<td class='num'>{_pl_num(bet.get('odd'))}</td>"
            f"<td class='num'>{_pl_num(bet.get('stake'))}</td>"
            f"<td>{escape(status)}</td>"
            f"<td class='num'>{_pl_num(bet.get('pnl'))}</td>"
            f"<td class='num'>{_pl_num(bet.get('saldo'))}</td>"
            f"<td class='num'>{_pct(bet.get('hit_overall'))}</td>"
            f"<td class='num'>{_pct(bet.get('hit_last10'))}</td>"
            "</tr>"
        )
    table_body = "".join(rows_html) or (
        '<tr><td colspan="11" class="empty-row">Brak zakładów.</td></tr>'
    )
    settle_open = ""
    open_rows = [b for b in bets if b.get("status") == "open"]
    if open_rows:
        items = []
        for bet in open_rows:
            bet_id = escape(str(bet.get("id", "")))
            items.append(
                f"<li>{escape(str(bet.get('home')))} – {escape(str(bet.get('away')))} "
                f"· Gol @ {_pl_num(bet.get('odd'))} "
                f"<form class='bet-settle inline' method='post' "
                f"action='/live/bets/{bet_id}/settle'>"
                f"<button type='submit' name='outcome' value='won'>W</button>"
                f"<button type='submit' name='outcome' value='lost'>P</button>"
                f"<button type='submit' name='outcome' value='void'>V</button>"
                f"</form></li>"
            )
        settle_open = (
            '<section class="open-block"><h2>Otwarte</h2><ul>'
            + "".join(items)
            + "</ul></section>"
        )
    charts = (
        f'<section class="charts">'
        f"{render_bets_saldo_chart(bets)}"
        f"{render_bets_hit_chart(bets)}"
        f"</section>"
    )
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zakłady next goal</title>
<style>
{_LIVE_CSS}
{_BETS_CSS}
</style>
</head>
<body>
<header>
  <h1><span class="dot" aria-hidden="true"></span> Zakłady next goal</h1>
  <div class="header-meta">
    {nav}
    <p class="sub">{escape(str(metrics.get('n', 0)))} typów · {when}</p>
  </div>
</header>
<main>
  <section class="metrics">
    <div><strong>{_pl_num(metrics.get('saldo'))}</strong><span>Saldo</span></div>
    <div><strong>{hit}</strong><span>Skuteczność</span></div>
    <div>
      <strong>{_pl_num(metrics.get('current_stake'))}</strong>
      <span>Bieżąca stawka</span>
    </div>
    <div><strong>{_pl_num(metrics.get('avg_odd'))}</strong><span>Śr. kurs</span></div>
    <div>
      <strong>
        {escape(str(metrics.get('wins', 0)))}/{escape(str(metrics.get('losses', 0)))}
      </strong>
      <span>W/P</span>
    </div>
    <div>
      <strong>{escape(str(metrics.get('open_count', 0)))}</strong>
      <span>Otwarte</span>
    </div>
  </section>
  {settle_open}
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Id</th><th>Data</th><th>Liga</th><th>Mecz</th>
          <th>Kurs</th><th>Stawka</th><th>Status</th><th>Wygrana</th>
          <th>Saldo</th><th>Hit</th><th>Ostatnie 10</th>
        </tr>
      </thead>
      <tbody>
        {table_body}
      </tbody>
    </table>
  </div>
  {charts}
</main>
{_REFRESH_SCRIPT}
</body>
</html>
"""


def _live_nav(active_nav: str) -> str:
    items = (
        ("all", "/live", "Wszystkie"),
        ("day", "/live/day", "Dzień"),
        ("next_goal", "/live/next-goal", "Następny gol"),
        ("bets", "/live/bets", "Zakłady"),
    )
    links: list[str] = []
    for key, href, label in items:
        cls = ' class="active"' if key == active_nav else ""
        links.append(f'<a href="{href}"{cls}>{escape(label)}</a>')
    return f'<nav class="nav" aria-label="Widok live">{"".join(links)}</nav>'
