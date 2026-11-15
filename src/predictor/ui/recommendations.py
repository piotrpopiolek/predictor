"""Recommendation card. Posts carry the decision, not a secret."""

from __future__ import annotations

from html import escape


def render_recommendations(
    rows: tuple[tuple[int, str, str, str], ...],
    waiting: tuple[tuple[int, str], ...] = (),
) -> str:
    pending = "".join(
        "<tr>"
        f'<td><a href="/advisor/recommendations?cycle={cycle_id}">{cycle_id}</a></td>'
        f"<td>{escape(name)}</td>"
        "<td>czekam</td>"
        "</tr>"
        for cycle_id, name in waiting
    )
    wait_table = ""
    if pending:
        wait_table = f"""
        <h2>Czekam na decyzję</h2>
        <table>
          <thead><tr><th>Cykl</th><th>Strategia</th><th>Stan</th></tr></thead>
          <tbody>{pending}</tbody>
        </table>
        """
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
        {wait_table}
        <table>
          <thead><tr><th>Kupon</th><th>Strategia</th><th>Status</th><th>Kurs</th></tr></thead>
          <tbody>{body}</tbody>
        </table>
        """,
    )


def render_waiting_card(
    *,
    cycle_id: int,
    status: str,
    lines: tuple[str, ...],
    notice: str,
) -> str:
    """Decision card before a recommendation coupon exists."""
    items = "".join(f"<li>{escape(line)}</li>" for line in lines)
    note = f"<p>{escape(notice)}</p>" if notice else ""
    forms = ""
    if status == "waiting_human":
        forms = f"""
        <form method="post" action="/advisor/recommendations/cycles/{cycle_id}/accept">
          <button type="submit">Akceptuj</button>
        </form>
        <form method="post" action="/advisor/recommendations/cycles/{cycle_id}/reject">
          <button type="submit">Odrzuć</button>
        </form>
        """
    if status == "waiting_human":
        state = "Czekam na decyzję. Kuponu rekomendacji jeszcze nie ma."
    else:
        state = "Czekanie zakończone."
    return _page(
        f"Cykl {cycle_id}",
        f"""
        <p>{escape(state)}</p>
        {note}
        <ul>{items}</ul>
        {forms}
        <p><a href="/advisor/recommendations">Lista</a></p>
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
          <button type="submit">Akceptuj</button>
        </form>
        <form method="post" action="/advisor/recommendations/{coupon_id}/modify">
          <label>Stawka <input name="stake" value="{escape(stake)}"></label>
          <label>Kursy fixture=kurs
            <input name="odds" placeholder="1=1.70,2=1.80">
          </label>
          <label>Usuń fixture <input name="remove" placeholder="2"></label>
          <button type="submit">Koryguj</button>
        </form>
        <form method="post" action="/advisor/recommendations/{coupon_id}/reject">
          <button type="submit">Odrzuć</button>
        </form>
        """
    if status in {"accepted", "modified"}:
        forms += f"""
        <form method="post" action="/advisor/recommendations/{coupon_id}/place">
          <button type="submit">Potwierdź zagranie</button>
        </form>
        <form method="post" action="/advisor/recommendations/{coupon_id}/not-placed">
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


def render_play_card(
    *,
    coupon_id: int,
    status: str,
    stake: str,
    combined: str,
    floor: str,
    lines: tuple[str, ...],
    notice: str,
) -> str:
    items = "".join(f"<li>{escape(line)}</li>" for line in lines)
    note = f"<p class=\"warn\">{escape(notice)}</p>" if notice else ""
    forms = ""
    if status == "pending":
        forms = f"""
        <form method="post" action="/advisor/sandbox/{coupon_id}/played">
          <label>Kurs łączny <input name="combined" value="{escape(combined)}"></label>
          <label>Stawka <input name="stake" value="{escape(stake)}"></label>
          <button type="submit">Zagrałem</button>
        </form>
        <form method="post" action="/advisor/sandbox/{coupon_id}/not-played">
          <button type="submit">Nie zagrałem</button>
        </form>
        """
    return _page(
        f"Kupon {coupon_id}",
        f"""
        <p>Pytanie o zagranie. Status {escape(status)}.
        Kurs z bazy {escape(combined)}. Stawka wirtualna {escape(stake)}.
        Próg {escape(floor)}.</p>
        {note}
        <ul>{items}</ul>
        {forms}
        <p><a href="/advisor/report">Raport</a></p>
        """,
    )


def render_strategies(
    rows: tuple[tuple[int, str, str, str], ...],
) -> str:
    body = "".join(
        "<tr>"
        f"<td>{escape(name)}</td><td>{escape(status)}</td><td>{escape(check)}</td>"
        f'<td><form method="post" action="/advisor/strategies/{strategy_id}/promote">'
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
    · <a href="/advisor/coupons">Kupony</a>
    · <a href="/advisor/recommendations">Rekomendacje</a>
    · <a href="/advisor/strategies">Strategie</a>
    · <a href="/advisor/browse">Tabele</a></p>
  <h1>{escape(title)}</h1>
  {body}
</main></body></html>
"""
