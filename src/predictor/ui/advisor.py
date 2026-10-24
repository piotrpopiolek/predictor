"""HTML for the sandbox yield report. Read-only."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from html import escape

from predictor.services.advisor.report import (
    BookLine,
    PairYield,
    StrategyChart,
    YieldReport,
)
from predictor.ui.live_board import render_bets_hit_chart, render_bets_saldo_chart

_PERIODS = (
    ("all", "Wszystko"),
    ("regulation", "Regulamin"),
    ("extra_time", "Dogrywka"),
)

_CHART_CSS = """
  :root {
    --card: #161b22;
    --line: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
  }
  .charts {
    display: grid;
    grid-template-columns: 1fr;
    gap: 16px;
    margin: 8px 0 28px;
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
  .chart-block h3 {
    margin: 24px 0 8px;
    font-size: 1rem;
    font-weight: 600;
  }
"""


def render_yield_report(report: YieldReport) -> str:
    rows = "".join(_book_row(line) for line in report.books)
    pairs = "".join(_pair_row(pair) for pair in report.pairs)
    charts = "".join(_strategy_charts(chart) for chart in report.charts)
    if not report.books:
        rows = '<tr><td colspan="9">Brak strategii piaskownicy.</td></tr>'
    if not report.pairs:
        pairs = (
            '<tr><td colspan="7">Brak reguły bazowej albo drugiej strategii.</td></tr>'
        )
    if not charts:
        charts = "<p>Za mało kuponów na wykresy.</p>"
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <title>Raport piaskownicy</title>
  <style>
    body {{ margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
      background: #0d1117; color: #e6edf3; }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }}
    a {{ color: #58a6ff; }}
    nav {{ display: flex; gap: 16px; margin: 16px 0 24px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 8px 0 28px; }}
    th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #30363d; }}
    th {{ color: #8b949e; font-weight: 600; }}
    p {{ color: #8b949e; }}
    {_CHART_CSS}
  </style>
</head>
<body>
<main>
  <p><a href="/live/next-goal">Następny gol</a>
    · <a href="/advisor/recommendations">Rekomendacje</a>
    · <a href="/advisor/strategies">Strategie</a>
    · <a href="/advisor/browse">Tabele</a></p>
  <h1>Raport piaskownicy</h1>
  <p>Yield netto to zysk po współczynniku 0,88 podzielony przez stawki kuponów
  wygranych i przegranych. Kupon nieważny oddaje stawkę i nie wchodzi do yieldu.
  Średni kurs to średnia arytmetyczna kursu łącznego kuponów wygranych i
  przegranych. Saldo odejmuje też stawki otwartych kuponów.</p>
  <nav>{_period_nav(report.period)}</nav>
  <h2>Portfele</h2>
  <table>
    <thead>
      <tr>
        <th>Strategia</th><th>Kupony</th><th>Trafienia</th><th>Trafność</th>
        <th>Śr. kurs</th><th>Stawka</th><th>Zysk netto</th>
        <th>Yield netto</th><th>Saldo</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <h2>Wykresy</h2>
  <p>Saldo i skuteczność narastająco, w tej samej formie co na stronie zakładów.</p>
  {charts}
  <h2>Te same mecze</h2>
  <p>Porównanie obejmuje mecze, na których obie strony mają rozliczony kupon
  z jednym zdarzeniem. Kupon łączony zostaje w portfelu i nie wchodzi tutaj.</p>
  <table>
    <thead>
      <tr>
        <th>Baseline</th><th>Strategia</th><th>Mecze</th>
        <th>Trafienia baseline</th><th>Trafienia strategii</th>
        <th>Yield baseline</th><th>Yield strategii</th>
      </tr>
    </thead>
    <tbody>{pairs}</tbody>
  </table>
</main>
</body>
</html>
"""


def _strategy_charts(chart: StrategyChart) -> str:
    bets = list(chart.points)
    return (
        f'<section class="chart-block">'
        f"<h3>{escape(chart.name)}</h3>"
        f'<div class="charts">'
        f"{render_bets_saldo_chart(bets)}"
        f"{render_bets_hit_chart(bets)}"
        f"</div></section>"
    )


def _period_nav(active: str) -> str:
    links: list[str] = []
    for key, label in _PERIODS:
        href = "/advisor/report" if key == "all" else f"/advisor/report?period={key}"
        if key == active:
            links.append(f"<strong>{label}</strong>")
        else:
            links.append(f'<a href="{href}">{label}</a>')
    return "".join(links)


def _book_row(line: BookLine) -> str:
    return (
        "<tr>"
        f"<td>{escape(line.name)}</td>"
        f"<td>{line.played}</td>"
        f"<td>{line.hits}</td>"
        f"<td>{_pct(line.hit_rate)}</td>"
        f"<td>{_odd(line.avg_odd)}</td>"
        f"<td>{_money(line.stakes)}</td>"
        f"<td>{_money(line.net_pnl)}</td>"
        f"<td>{_pct(line.yield_net)}</td>"
        f"<td>{_money(line.balance)}</td>"
        "</tr>"
    )


def _pair_row(pair: PairYield) -> str:
    return (
        "<tr>"
        f"<td>{escape(pair.baseline_name)}</td>"
        f"<td>{escape(pair.strategy_name)}</td>"
        f"<td>{pair.matches}</td>"
        f"<td>{pair.baseline_hits}</td>"
        f"<td>{pair.strategy_hits}</td>"
        f"<td>{_pct(pair.baseline_yield)}</td>"
        f"<td>{_pct(pair.strategy_yield)}</td>"
        "</tr>"
    )


def render_recommendation_cards(cards: list[dict[str, object]]) -> str:
    """Cards on the board. The operator types the token."""
    if not cards:
        return ""
    blocks = "".join(_recommendation_card(card) for card in cards)
    return f'<section class="reco"><h2>Rekomendacje</h2>{blocks}</section>'


def _recommendation_card(card: dict[str, object]) -> str:
    raw_id = card["id"]
    assert isinstance(raw_id, int)
    coupon_id = raw_id
    legs = card["legs"]
    leg_html = []
    assert isinstance(legs, list)
    for leg in legs:
        assert isinstance(leg, dict)
        raw_fixture = leg["fixture_id"]
        assert isinstance(raw_fixture, int)
        fixture_id = raw_fixture
        leg_html.append(
            "<li>"
            f"Mecz {fixture_id}, {escape(str(leg['score']))}, "
            f"Over {escape(str(leg['line']))} @ {escape(str(leg['odd']))} "
            f"(propozycja {escape(str(leg['proposed']))})"
            f'<label>kurs <input name="odd_{fixture_id}" '
            f'value="{escape(str(leg["odd"]))}"></label>'
            f'<label><input type="checkbox" name="remove_{fixture_id}"> usuń</label>'
            "</li>"
        )
    warning = ""
    if card.get("below"):
        warning = (
            '<p class="warn">Kurs łączny jest poniżej progu strategii. '
            "Potwierdzenie jest możliwe.</p>"
        )
    action = f"/advisor/recommendations/{coupon_id}"
    return (
        "<article>"
        f"<h3>Kupon {coupon_id} · {escape(str(card['status']))}</h3>"
        f"<p>Stawka {escape(str(card['stake']))}, "
        f"kurs {escape(str(card['combined']))}. "
        f"Ważny jeszcze {escape(str(card['remaining']))} s.</p>"
        f"{warning}<ul>{''.join(leg_html)}</ul>"
        f'<form method="post" action="{action}/accept">'
        '<label>token <input name="token" type="password" autocomplete="off"></label>'
        '<button type="submit">Akceptuj</button></form>'
        f'<form method="post" action="{action}/modify">'
        '<label>token <input name="token" type="password" autocomplete="off"></label>'
        f'<label>stawka <input name="stake" '
        f'value="{escape(str(card["stake"]))}"></label>'
        f"{''.join(leg_html)}"
        '<button type="submit">Koryguj</button></form>'
        f'<form method="post" action="{action}/place">'
        '<label>token <input name="token" type="password" autocomplete="off"></label>'
        '<button type="submit">Potwierdź zagranie</button></form>'
        f'<form method="post" action="{action}/reject">'
        '<label>token <input name="token" type="password" autocomplete="off"></label>'
        '<button type="submit">Odrzuć</button></form>'
        f'<form method="post" action="{action}/not-placed">'
        '<label>token <input name="token" type="password" autocomplete="off"></label>'
        '<button type="submit">Nie zagrano</button></form>'
        "</article>"
    )


def _money(value: Decimal) -> str:
    shown = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(shown, "f").replace(".", ",")


def _odd(value: Decimal | None) -> str:
    if value is None:
        return "—"
    shown = value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return format(shown, "f").replace(".", ",")


def _pct(rate: Decimal | None) -> str:
    if rate is None:
        return "—"
    shown = (rate * Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(shown, "f").replace(".", ",") + "%"
