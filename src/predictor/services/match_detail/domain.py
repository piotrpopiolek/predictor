"""match_detail domain models and pure helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from predictor.constants import (
    FINISHED_FIXTURE_STATUSES,
    IN_PLAY_FIXTURE_STATUSES,
)
from predictor.services.lineup_strength import LineupStrength
from predictor.services.live_board import (
    LiveMatch,
)


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
    form_results: tuple[tuple[str, tuple[DetailH2H, ...]], ...] = ()
    home_strength: LineupStrength | None = None
    away_strength: LineupStrength | None = None

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
