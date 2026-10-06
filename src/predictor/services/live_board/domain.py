"""live_board domain models and pure helpers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import (
    UTC,
    date,
    datetime,
)
from decimal import (
    Decimal,
    InvalidOperation,
)
from typing import Any

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

_GOAL_MINUTE_BIN_MID = {
    "0-15": 7.5,
    "16-30": 23.0,
    "31-45": 38.0,
    "46-60": 53.0,
    "61-75": 68.0,
    "76-90": 83.0,
    "91-105": 98.0,
    "106-120": 113.0,
}

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

def avg_minute_from_goal_bins(raw: object) -> float | None:
    """Weighted midpoint of API goal-for minute bins (0-15, 16-30, …)."""
    if not isinstance(raw, dict):
        return None
    total = 0
    weighted = 0.0
    for key, mid in _GOAL_MINUTE_BIN_MID.items():
        cell = raw.get(key)
        if not isinstance(cell, dict):
            continue
        value = cell.get("total")
        if value is None:
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        total += count
        weighted += count * mid
    if not total:
        return None
    return weighted / total

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
