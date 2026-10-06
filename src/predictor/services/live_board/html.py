"""HTML renderers for live_board."""

from __future__ import annotations

from datetime import (
    date,
    datetime,
    timedelta,
)
from decimal import (
    Decimal,
    InvalidOperation,
)
from html import escape
from itertools import groupby
from typing import Any

from predictor.services.live_board.domain import (
    _ET_STATUSES,
    _HT_STATUSES,
    _PEN_STATUSES,
    FORM_LAST_MATCHES,
    REFRESH_SECONDS,
    LiveMatch,
    TeamGoalForm,
    _kickoff_utc,
    _match_noun,
    order_day_matches,
    sort_matches_by_clock,
)


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

def _board_price(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        text = f"{Decimal(raw).quantize(Decimal('0.01')):.2f}"
    except InvalidOperation:
        text = raw
    return f'<span class="team-odd">{escape(text)}</span>'

def _opening_price(match: LiveMatch, side: str) -> str:
    quote = match.prematch
    if quote is None:
        return ""
    raw = quote.home if side == "home" else quote.away
    return _board_price(raw)

def _opening_draw(match: LiveMatch) -> str:
    quote = match.prematch
    if quote is None:
        return ""
    price = _board_price(quote.draw)
    if not price:
        return ""
    return price.replace('class="team-odd"', 'class="score-odd"', 1)

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
  .team-odd, .score-odd {
    display: block;
    margin-top: 2px;
    font-weight: 650;
    font-variant-numeric: tabular-nums;
    font-size: 0.92rem;
  }
  .score-odd { text-align: center; }
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

def _token_field(operator_token: str | None) -> str:
    if not operator_token:
        return ""
    return (
        '<input type="hidden" name="token" '
        f'value="{escape(operator_token, quote=True)}">'
    )


def _match_card(
    match: LiveMatch,
    *,
    allow_bet: bool = False,
    open_bet: dict[str, Any] | None = None,
    current_stake: str | None = None,
    show_kickoff: bool = False,
    operator_token: str | None = None,
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
        bet_block = _bet_block(
            match,
            open_bet=open_bet,
            current_stake=current_stake,
            operator_token=operator_token,
        )
    return f"""
<article class="match" data-fixture-id="{match.fixture_id}">
  <a class="match-link" href="/live/match/{match.fixture_id}">
  <div class="headline">
    <div class="team home">
      {_img(match.home_logo, match.home)}
      <div class="team-text">
        <span class="name">{home}{_reds(match.home_reds)}</span>
        {_opening_price(match, "home")}
        {_formation(match.home_formation)}
      </div>
    </div>
    <div class="scoreblock">
      <div class="score">{_score(match.goals_home)}–{_score(match.goals_away)}</div>
      {_opening_draw(match)}
      <div class="meta" title="{long_status}">
        {kickoff_html}<span class="clock">{clock}</span>
        <span class="status">{status}</span>
      </div>
    </div>
    <div class="team away">
      {_img(match.away_logo, match.away)}
      <div class="team-text">
        <span class="name">{_reds(match.away_reds)}{away}</span>
        {_opening_price(match, "away")}
        {_formation(match.away_formation)}
      </div>
    </div>
  </div>
    {scorers}
  {_facts(match)}
  {_stats_line(match)}
  {_form_line(match)}
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
    operator_token: str | None = None,
) -> str:
    token = _token_field(operator_token)
    if open_bet is not None:
        odd = escape(str(open_bet.get("odd", "")))
        stake = escape(str(open_bet.get("stake", "")))
        bet_id = escape(str(open_bet.get("id", "")))
        return f"""
  <div class="bet-open" data-open-bet="1">
    <span class="bet-badge">Otwarty · Gol @ {odd} · stawka {stake}</span>
    <form class="bet-settle" method="post" action="/live/bets/{bet_id}/settle">
      {token}
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
      {token}
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
    operator_token: str | None = None,
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
                operator_token=operator_token,
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
    operator_token: str | None = None,
) -> str:
    metrics = payload.get("metrics") or {}
    bets = list(payload.get("bets") or [])
    when = escape(generated_at.strftime("%H:%M:%S UTC"))
    nav = _live_nav("bets")
    hit = _pct(metrics.get("hit_rate"))
    token = _token_field(operator_token)
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
                f"{token}"
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
