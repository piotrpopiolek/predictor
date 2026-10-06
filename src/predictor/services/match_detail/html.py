"""HTML renderers for match_detail."""

from __future__ import annotations

from datetime import datetime
from decimal import (
    Decimal,
    InvalidOperation,
)
from html import escape

from predictor.services.lineup_strength import LineupStrength
from predictor.services.live_board import (
    FORM_LAST_MATCHES,
    REFRESH_SECONDS,
    LiveMatch,
    PrematchOdds,
    TeamGoalForm,
    _img,
    _live_nav,
    _score,
)
from predictor.services.match_detail.domain import (
    DetailH2H,
    DetailPlayer,
    GroupTable,
    MatchDetail,
    OddsMarket,
    RankedSlot,
    _fmt_odd,
)

_EVENT_LABELS = {
    "Goal": "Gol",
    "Card": "Kartka",
    "subst": "Zmiana",
    "Var": "VAR",
}


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
    {_team_side(match.home_logo, match.home, _opening_odd(match, "home"), home=True)}
    <div class="scoreblock">
      <div class="score">{_score(match.goals_home)}–{_score(match.goals_away)}</div>
      {_draw_odd(match)}
      <div class="meta"><span class="clock">{escape(match.clock)}</span>
        <span>{escape(match.status_short)}</span></div>
    </div>
    {_team_side(match.away_logo, match.away, _opening_odd(match, "away"), home=False)}
  </div>
  {chips}
  {periods}
</section>
{_table_card(detail)}
{_events_card(detail)}
{_stats_card(detail)}
{_attack_subs_card(detail)}
{_goal_price_card(detail)}
{_form_card(detail)}
{_lineups_card(detail)}
{_odds_card(detail)}
{_prediction_card(detail)}
{_h2h_card(detail)}
<p class="sub">Odświeżono {escape(generated_at.strftime("%H:%M:%S UTC"))}</p>
"""


def _opening_quote(match: LiveMatch) -> PrematchOdds | None:
    quote = match.prematch
    if quote is None:
        return None
    if quote.source == "prematch" or quote.home or quote.away:
        return quote
    return None


def _price(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return _fmt_odd(Decimal(raw))
    except InvalidOperation:
        return raw


def _opening_odd(match: LiveMatch, side: str) -> str | None:
    quote = _opening_quote(match)
    if quote is None:
        return None
    raw = quote.home if side == "home" else quote.away
    return _price(raw)


def _draw_odd(match: LiveMatch) -> str:
    quote = _opening_quote(match)
    if quote is None:
        return ""
    priced = _price(quote.draw)
    if not priced:
        return ""
    return f'<div class="score-odd">{escape(priced)}</div>'


def _team_side(logo: str | None, name: str, odd: str | None, *, home: bool) -> str:
    price = f'<span class="team-odd">{escape(odd)}</span>' if odd else ""
    identity = (
        f'<span class="team-id"><span class="team-name">{escape(name)}</span>'
        f"{price}</span>"
    )
    mark = _img(logo, name)
    side = "home" if home else "away"
    inner = f"{mark}{identity}" if home else f"{identity}{mark}"
    return f'<div class="team {side}">{inner}</div>'


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
        match = detail.match
        rows = []
        for event in detail.events:
            who = event.player or ""
            if event.assist and event.event_type == "Goal":
                who = f"{who} ({event.assist})" if who else event.assist
            label = _EVENT_LABELS.get(event.event_type, event.event_type)
            detail_text = event.detail or ""
            club = match.home if event.side == "home" else match.away
            side = "home" if event.side == "home" else "away"
            what = escape(label)
            if detail_text:
                what = f"{what} <em>{escape(detail_text)}</em>"
            who_html = f'<span class="who">{escape(who)}</span>' if who else ""
            rows.append(
                f'<li class="{side}">'
                f'<span class="minute">{escape(event.clock)}</span>'
                f'<span class="ev">'
                f'<span class="club">{escape(club)}</span>'
                f'<span class="what">{what}</span>'
                f"{who_html}</span></li>"
            )
        inner = f'<ol class="timeline">{"".join(rows)}</ol>'
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
      {_strength_block(detail.home_strength)}
      {_player_list(detail.home_xi)}
      {_bench(detail.home_bench)}
    </div>
    <div>
      <h3>{escape(match.away)}</h3>
      {_strength_block(detail.away_strength)}
      {_player_list(detail.away_xi)}
      {_bench(detail.away_bench)}
    </div>
  </div>
</section>
"""


def _strength_block(strength: LineupStrength | None) -> str:
    if strength is None or strength.label is None:
        return ""
    note = ""
    if strength.note:
        note = f'<p class="strength-note">{escape(strength.note)}</p>'
    missing = ""
    if strength.missing:
        text = "Nie gra: " + ", ".join(strength.missing)
        missing = f'<p class="strength-note">{escape(text)}</p>'
    return f'<p class="strength">{escape(strength.label)}</p>{note}{missing}'


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


def _form_bits(form: TeamGoalForm | None) -> str:
    if form is None:
        return ""
    bits = [f"{form.avg_goals:.2f} gola"]
    if form.minute_label is not None:
        bits.append(f"śr. {form.minute_label}")
    if form.matches != FORM_LAST_MATCHES:
        bits.append(f"{form.matches} m.")
    return " · ".join(escape(bit) for bit in bits)


def _form_card(detail: MatchDetail) -> str:
    match = detail.match
    by_name = {name: rows for name, rows in detail.form_results if rows}
    columns: list[str] = []
    for name, form in ((match.home, match.form_home), (match.away, match.form_away)):
        rows = by_name.get(name, ())
        bits = _form_bits(form)
        if not bits and not rows:
            continue
        summary = f'<p class="muted">{bits}</p>' if bits else ""
        listing = f"<ul class='h2h'>{_h2h_rows(rows)}</ul>" if rows else ""
        columns.append(f"<div><h3>{escape(name)}</h3>{summary}{listing}</div>")
    if not columns:
        return ""
    body = f'<div class="form-cols">{"".join(columns)}</div>'
    return (
        '<section class="card"><h2>Ostatnie '
        f"{FORM_LAST_MATCHES} meczów</h2>{body}</section>"
    )


def _h2h_rows(items: tuple[DetailH2H, ...] | list[DetailH2H]) -> str:
    return "".join(
        "<li><span class='when'>"
        + escape(item.when)
        + "</span><span>"
        + escape(item.home)
        + " "
        + escape(item.score)
        + " "
        + escape(item.away)
        + "</span></li>"
        for item in items
    )


def _h2h_card(detail: MatchDetail) -> str:
    if not detail.h2h:
        return ""
    return (
        '<section class="card"><h2>Bezpośrednie</h2>'
        f"<ul class='h2h'>{_h2h_rows(detail.h2h)}</ul></section>"
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
  .team-id {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .team.away .team-id { align-items: flex-end; }
  .team-name { overflow: hidden; text-overflow: ellipsis; }
  .team-odd, .score-odd {
    font-variant-numeric: tabular-nums;
    font-weight: 650;
    font-size: 0.95rem;
  }
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
  .timeline li {
    display: grid;
    grid-template-columns: 3.2rem 1fr;
    gap: 10px;
    padding: 8px 0;
    border-top: 1px solid var(--line);
    font-size: 0.88rem;
    align-items: start;
  }
  .timeline li.away {
    grid-template-columns: 1fr 3.2rem;
  }
  .timeline li.away .minute { order: 2; text-align: right; }
  .timeline li.away .ev { order: 1; text-align: right; }
  .timeline .ev {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .timeline .club {
    font-weight: 650;
    font-size: 0.82rem;
  }
  .timeline li.home .club { color: var(--text); }
  .timeline li.away .club { color: var(--muted); }
  .h2h li, .compare li, .xi-list li {
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
  .tables, .form-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  .tables .kicker { margin-bottom: 8px; }
  .form-cols h3 { margin-bottom: 4px; }
  .form-cols .muted { margin: 0 0 8px; font-size: 0.82rem; }
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
  .strength { margin: 0 0 4px; font-size: 0.82rem; font-weight: 650; }
  .strength-note { margin: 0 0 8px; color: var(--muted); font-size: 0.75rem; }
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
    .grid, .xi, .headline, .tables, .form-cols, .split-grid {
      grid-template-columns: 1fr;
    }
    .team.away { flex-direction: row; text-align: left; }
    .team.away .team-id { align-items: flex-start; }
    .scoreblock { order: -1; }
  }
"""
