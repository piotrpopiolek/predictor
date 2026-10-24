"""Recommendation card. The operator types the token; the page does not contain it."""

from __future__ import annotations

from html import escape


def render_recommendations(rows: tuple[tuple[int, str, str, str], ...]) -> str:
    body = "".join(
        "<tr>"
        f'<td><a href="/advisor/recommendations/{coupon_id}">{coupon_id}</a></td>'
        f"<td>{escape(name)}</td>"
        f"<td>{escape(status)}</td>"
        f"<td>{escape(odd)}</td>"
        "</tr>"
        for coupon_id, name, status, odd in rows
    )
    if not body:
        body = '<tr><td colspan="4">Brak rekomendacji.</td></tr>'
    return _page(
        "Rekomendacje",
        f"""
        <table>
          <thead><tr><th>Kupon</th><th>Strategia</th><th>Status</th><th>Kurs</th></tr></thead>
          <tbody>{body}</tbody>
        </table>
        """,
    )


def render_recommendation_card(
    *,
    coupon_id: int,
    status: str,
    stake: str,
    combined: str,
    floor: str,
    lines: tuple[str, ...],
    below: bool,
) -> str:
    warning = ""
    if below:
        warning = (
            '<p class="warn">Kurs łączny jest poniżej progu strategii. '
            "Potwierdzenie jest możliwe.</p>"
        )
    legs = "".join(f"<li>{escape(line)}</li>" for line in lines)
    forms = ""
    if status in {"proposed", "accepted", "modified"}:
        forms = f"""
        <form method="post" action="/advisor/recommendations/{coupon_id}/accept">
          <label>Token <input type="password" name="token" autocomplete="off"></label>
          <button type="submit">Akceptuj</button>
        </form>
        <form method="post" action="/advisor/recommendations/{coupon_id}/modify">
          <label>Token <input type="password" name="token" autocomplete="off"></label>
          <label>Stawka <input name="stake" value="{escape(stake)}"></label>
          <label>Kursy fixture=kurs
            <input name="odds" placeholder="1=1.70,2=1.80">
          </label>
          <label>Usuń fixture <input name="remove" placeholder="2"></label>
          <button type="submit">Koryguj</button>
        </form>
        <form method="post" action="/advisor/recommendations/{coupon_id}/reject">
          <label>Token <input type="password" name="token" autocomplete="off"></label>
          <button type="submit">Odrzuć</button>
        </form>
        """
    if status in {"accepted", "modified"}:
        forms += f"""
        <form method="post" action="/advisor/recommendations/{coupon_id}/place">
          <label>Token <input type="password" name="token" autocomplete="off"></label>
          <button type="submit">Potwierdź zagranie</button>
        </form>
        <form method="post" action="/advisor/recommendations/{coupon_id}/not-placed">
          <label>Token <input type="password" name="token" autocomplete="off"></label>
          <button type="submit">Nie zagrano</button>
        </form>
        """
    return _page(
        f"Kupon {coupon_id}",
        f"""
        <p>Status {escape(status)}. Stawka {escape(stake)}.
        Kurs {escape(combined)}. Próg {escape(floor)}.</p>
        {warning}
        <ul>{legs}</ul>
        {forms}
        <p><a href="/advisor/recommendations">Lista</a></p>
        """,
    )


def render_strategies(
    rows: tuple[tuple[int, str, str, str], ...],
) -> str:
    body = "".join(
        "<tr>"
        f"<td>{escape(name)}</td><td>{escape(status)}</td><td>{escape(check)}</td>"
        f'<td><form method="post" action="/advisor/strategies/{strategy_id}/promote">'
        f'<input type="password" name="token" autocomplete="off">'
        f'<button type="submit">Przenieś</button></form></td>'
        "</tr>"
        for strategy_id, name, status, check in rows
    )
    if not body:
        body = '<tr><td colspan="4">Brak strategii.</td></tr>'
    return _page(
        "Strategie",
        f"""
        <table>
          <thead><tr><th>Nazwa</th><th>Status</th><th>Kryteria</th><th></th></tr></thead>
          <tbody>{body}</tbody>
        </table>
        """,
    )


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <title>{escape(title)}</title>
  <style>
    body {{ margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
      background: #0d1117; color: #e6edf3; }}
    main {{ max-width: 800px; margin: 0 auto; padding: 24px 16px 48px; }}
    a {{ color: #58a6ff; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #30363d; }}
    .warn {{ color: #f2cc60; }}
    form {{ margin: 12px 0; }}
  </style>
</head>
<body><main>
  <p><a href="/advisor/report">Raport</a>
    · <a href="/advisor/recommendations">Rekomendacje</a>
    · <a href="/advisor/strategies">Strategie</a>
    · <a href="/advisor/browse">Tabele</a></p>
  <h1>{escape(title)}</h1>
  {body}
</main></body></html>
"""
