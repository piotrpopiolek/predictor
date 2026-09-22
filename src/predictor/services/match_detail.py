"""Read-only match page: events, stats, lineups, prediction, odds, H2H."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import aliased

from predictor.models.catalog import Player
from predictor.models.children import (
    FixtureEvent,
    FixtureLineupPlayer,
    FixtureStatistic,
)
from predictor.models.fixtures import Fixture, Team
from predictor.models.predictions import Prediction, PredictionH2H
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
    .grid, .xi, .headline { grid-template-columns: 1fr; }
    .team.away { flex-direction: row; text-align: left; }
    .scoreblock { order: -1; }
  }
"""
