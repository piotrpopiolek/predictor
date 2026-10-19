"""HTML for the sandbox yield report. Read-only."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from html import escape

from predictor.services.advisor.report import BookLine, PairYield, YieldReport

_PERIODS = (
    ("all", "Wszystko"),
    ("regulation", "Regulamin"),
    ("extra_time", "Dogrywka"),
)


def render_yield_report(report: YieldReport) -> str:
    rows = "".join(_book_row(line) for line in report.books)
    pairs = "".join(_pair_row(pair) for pair in report.pairs)
    if not report.books:
        rows = '<tr><td colspan="8">Brak strategii piaskownicy.</td></tr>'
    if not report.pairs:
        pairs = (
            '<tr><td colspan="7">Brak reguły bazowej albo drugiej strategii.</td></tr>'
        )
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <title>Raport piaskownicy</title>
  <style>
    body {{ margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
      background: #0d1117; color: #e6edf3; }}
    main {{ max-width: 960px; margin: 0 auto; padding: 24px 16px 48px; }}
    a {{ color: #58a6ff; }}
    nav {{ display: flex; gap: 16px; margin: 16px 0 24px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 8px 0 28px; }}
    th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #30363d; }}
    th {{ color: #8b949e; font-weight: 600; }}
    p {{ color: #8b949e; }}
  </style>
</head>
<body>
<main>
  <p><a href="/live/next-goal">Następny gol</a></p>
  <h1>Raport piaskownicy</h1>
  <p>Yield netto to zysk po współczynniku 0,88 podzielony przez stawki kuponów
  wygranych i przegranych. Kupon nieważny oddaje stawkę i nie wchodzi do yieldu.</p>
  <nav>{_period_nav(report.period)}</nav>
  <h2>Portfele</h2>
  <table>
    <thead>
      <tr>
        <th>Strategia</th><th>Kupony</th><th>Trafienia</th><th>Trafność</th>
        <th>Stawka</th><th>Zysk netto</th><th>Yield netto</th><th>Saldo</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
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


def _money(value: Decimal) -> str:
    shown = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(shown, "f").replace(".", ",")


def _pct(rate: Decimal | None) -> str:
    if rate is None:
        return "—"
    shown = (rate * Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(shown, "f").replace(".", ",") + "%"
