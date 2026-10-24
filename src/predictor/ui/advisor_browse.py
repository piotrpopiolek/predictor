"""HTML for the read-only advisor table browser."""

from __future__ import annotations

import json
from html import escape
from typing import Any

from predictor.services.advisor.browse import TABLE_NAMES


def render_browse_index(tables: list[dict[str, Any]]) -> str:
    body = "".join(
        "<tr>"
        f'<td><a href="/advisor/browse/{escape(str(item["table"]))}">'
        f'{escape(str(item["table"]))}</a></td>'
        f"<td>{int(item['rows'])}</td>"
        "</tr>"
        for item in tables
    )
    if not body:
        body = '<tr><td colspan="2">Brak tabel.</td></tr>'
    return _page(
        "Tabele advisor",
        f"""
        <table>
          <thead><tr><th>Tabela</th><th>Wiersze</th></tr></thead>
          <tbody>{body}</tbody>
        </table>
        """,
    )


def render_browse_list(
    *,
    table: str,
    rows: list[dict[str, Any]],
    filters: dict[str, str],
    limit: int,
    next_before_id: int | None,
) -> str:
    headers = _list_headers(table)
    head = "".join(f"<th>{escape(label)}</th>" for label in headers)
    body = "".join(_list_row(table, row) for row in rows)
    if not body:
        body = f'<tr><td colspan="{len(headers)}">Brak wierszy.</td></tr>'
    filter_bits = "".join(
        f"<code>{escape(key)}={escape(value)}</code> " for key, value in filters.items()
    )
    filter_line = (
        f"<p>Filtry: {filter_bits}</p>" if filter_bits else "<p>Bez filtrów.</p>"
    )
    more = ""
    if next_before_id is not None:
        qs = _query(filters, before_id=next_before_id, limit=limit)
        more = f'<p><a href="/advisor/browse/{escape(table)}{qs}">Następne</a></p>'
    return _page(
        table,
        f"""
        <p><a href="/advisor/browse">Wszystkie tabele</a></p>
        {filter_line}
        <table>
          <thead><tr>{head}</tr></thead>
          <tbody>{body}</tbody>
        </table>
        {more}
        """,
    )


def render_browse_row(*, table: str, row: dict[str, Any]) -> str:
    cells = "".join(
        f"<tr><th>{escape(key)}</th><td>{_cell(value)}</td></tr>"
        for key, value in row.items()
        if not _is_nested_blob(key, value)
    )
    extras = "".join(
        f"<h2>{escape(key)}</h2><pre>{escape(_pretty(value))}</pre>"
        for key, value in row.items()
        if _is_nested_blob(key, value)
    )
    reco = ""
    path = row.get("recommendation_path")
    if isinstance(path, str):
        reco = f'<p><a href="{escape(path)}">Karta rekomendacji</a></p>'
    return _page(
        f"{table} #{row.get('id')}",
        f"""
        <p><a href="/advisor/browse/{escape(table)}">Lista</a></p>
        {reco}
        <table>
          <tbody>{cells}</tbody>
        </table>
        {extras}
        """,
    )


def _list_headers(table: str) -> tuple[str, ...]:
    base = {
        "advisor_strategies": ("id", "name", "status", "kind", "created_at"),
        "advisor_strategy_versions": (
            "id",
            "strategy",
            "version",
            "min_odd",
            "max_legs",
            "created_at",
        ),
        "advisor_strategy_events": (
            "id",
            "strategy",
            "from",
            "to",
            "source",
            "created_at",
        ),
        "advisor_cycles": (
            "id",
            "strategy",
            "mode",
            "validation",
            "model",
            "cost",
            "matches",
            "started_at",
        ),
        "advisor_decisions": (
            "id",
            "strategy",
            "match",
            "decision",
            "period",
            "state",
            "n",
            "odd",
            "cycle",
            "created_at",
        ),
        "advisor_counterfactuals": (
            "id",
            "decision",
            "strategy",
            "match",
            "goal",
            "odd",
            "recorded_at",
        ),
        "advisor_coupons": (
            "id",
            "strategy",
            "mode",
            "status",
            "stake",
            "odd",
            "legs",
            "created_at",
        ),
        "advisor_legs": (
            "id",
            "coupon",
            "strategy",
            "match",
            "period",
            "n",
            "odd",
            "status",
            "created_at",
        ),
        "advisor_coupon_events": (
            "id",
            "coupon",
            "strategy",
            "from",
            "to",
            "source",
            "legs",
            "created_at",
        ),
        "advisor_settlement_events": (
            "id",
            "coupon",
            "strategy",
            "leg",
            "from",
            "to",
            "source",
            "created_at",
        ),
        "advisor_langfuse_outbox": (
            "id",
            "cycle",
            "strategy",
            "status",
            "attempts",
            "error",
            "created_at",
        ),
        "advisor_telegram_outbox": (
            "id",
            "kind",
            "status",
            "attempts",
            "error",
            "created_at",
        ),
    }
    return base.get(table, ("id",))


def _list_row(table: str, row: dict[str, Any]) -> str:
    cells = _list_cells(table, row)
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


def _list_cells(table: str, row: dict[str, Any]) -> tuple[str, ...]:
    rid = _id_link(table, row.get("id"))
    if table == "advisor_strategies":
        return (
            rid,
            escape(str(row.get("name") or "")),
            escape(str(row.get("status") or "")),
            escape(str(row.get("kind") or "")),
            escape(str(row.get("created_at") or "")),
        )
    if table == "advisor_strategy_versions":
        return (
            rid,
            escape(str(row.get("strategy_name") or row.get("strategy_id") or "")),
            escape(str(row.get("version_no") or "")),
            escape(str(row.get("min_combined_odd") or "")),
            escape(str(row.get("max_legs") or "")),
            escape(str(row.get("created_at") or "")),
        )
    if table == "advisor_strategy_events":
        return (
            rid,
            escape(str(row.get("strategy_name") or row.get("strategy_id") or "")),
            escape(str(row.get("previous_status") or "")),
            escape(str(row.get("new_status") or "")),
            escape(str(row.get("source") or "")),
            escape(str(row.get("created_at") or "")),
        )
    if table == "advisor_cycles":
        return (
            rid,
            escape(str(row.get("strategy_name") or "")),
            escape(str(row.get("mode") or "")),
            escape(str(row.get("validation_status") or "")),
            escape(str(row.get("model") or "")),
            escape(str(row.get("cost_pln") or "")),
            escape(_match_list(row.get("matches"))),
            escape(str(row.get("started_at") or "")),
        )
    if table == "advisor_decisions":
        cycle = row.get("cycle") if isinstance(row.get("cycle"), dict) else None
        cycle_text = ""
        if cycle is not None:
            cycle_text = f"{cycle.get('mode')} {cycle.get('validation_status')}"
        return (
            rid,
            escape(str(row.get("strategy_name") or "")),
            _match_cell(row.get("match"), row.get("fixture_id")),
            escape(str(row.get("decision") or "")),
            escape(str(row.get("period") or "")),
            escape(f"{row.get('goals_home')}:{row.get('goals_away')}"),
            escape(str(row.get("n") if row.get("n") is not None else "")),
            escape(str(row.get("odd_at_eval") or "")),
            escape(cycle_text),
            escape(str(row.get("created_at") or "")),
        )
    if table == "advisor_counterfactuals":
        raw_decision = row.get("decision")
        decision: dict[str, Any] = (
            raw_decision if isinstance(raw_decision, dict) else {}
        )
        return (
            rid,
            escape(str(decision.get("decision") or "")),
            escape(str(row.get("strategy_name") or "")),
            _match_cell(row.get("match"), decision.get("fixture_id")),
            escape(str(row.get("goal_scored"))),
            escape(str(row.get("odd") or "")),
            escape(str(row.get("recorded_at") or "")),
        )
    if table == "advisor_coupons":
        return (
            rid,
            escape(_strategy_version_label(row)),
            escape(str(row.get("mode") or "")),
            escape(str(row.get("status") or "")),
            escape(str(row.get("stake") or "")),
            escape(str(row.get("combined_odd") or "")),
            escape(_match_list(_leg_matches(row.get("legs")))),
            escape(str(row.get("created_at") or "")),
        )
    if table == "advisor_legs":
        return (
            rid,
            escape(str(row.get("coupon_id") or "")),
            escape(str(row.get("strategy_name") or "")),
            _match_cell(row.get("match"), row.get("fixture_id")),
            escape(str(row.get("period") or "")),
            escape(str(row.get("n") if row.get("n") is not None else "")),
            escape(str(row.get("odd") or "")),
            escape(str(row.get("status") or "")),
            escape(str(row.get("created_at") or "")),
        )
    if table == "advisor_coupon_events":
        return (
            rid,
            escape(str(row.get("coupon_id") or "")),
            escape(str(row.get("strategy_name") or "")),
            escape(str(row.get("previous_status") or "")),
            escape(str(row.get("new_status") or "")),
            escape(str(row.get("source") or "")),
            escape(_match_list(_leg_matches(row.get("legs")))),
            escape(str(row.get("created_at") or "")),
        )
    if table == "advisor_settlement_events":
        leg = row.get("leg") if isinstance(row.get("leg"), dict) else None
        leg_text = ""
        if leg is not None:
            match = leg.get("match") if isinstance(leg.get("match"), dict) else None
            label = match.get("label") if match else leg.get("fixture_id")
            leg_text = f"{label} {leg.get('period')} Over {leg.get('n')}+0.5"
        return (
            rid,
            escape(str(row.get("coupon_id") or "")),
            escape(str(row.get("strategy_name") or "")),
            escape(leg_text),
            escape(str(row.get("previous_status") or "")),
            escape(str(row.get("new_status") or "")),
            escape(str(row.get("source") or "")),
            escape(str(row.get("created_at") or "")),
        )
    if table == "advisor_langfuse_outbox":
        return (
            rid,
            escape(str(row.get("cycle_id") or "")),
            escape(str(row.get("strategy_name") or "")),
            escape(str(row.get("status") or "")),
            escape(str(row.get("attempts") or "")),
            escape(str(row.get("last_error") or "")),
            escape(str(row.get("created_at") or "")),
        )
    if table == "advisor_telegram_outbox":
        return (
            rid,
            escape(str(row.get("kind") or "")),
            escape(str(row.get("status") or "")),
            escape(str(row.get("attempts") or "")),
            escape(str(row.get("last_error") or "")),
            escape(str(row.get("created_at") or "")),
        )
    return (rid,)


def _strategy_version_label(row: dict[str, Any]) -> str:
    name = str(row.get("strategy_name") or "")
    version = row.get("version_no")
    if version is None:
        return name
    return f"{name} v{version}".strip()


def _id_link(table: str, value: Any) -> str:
    if value is None:
        return ""
    return f'<a href="/advisor/browse/{escape(table)}/{int(value)}">{int(value)}</a>'


def _match_cell(match: Any, fixture_id: Any) -> str:
    if isinstance(match, dict):
        label = escape(str(match.get("label") or ""))
        path = match.get("path")
        score = escape(str(match.get("score") or ""))
        league = escape(str(match.get("league") or ""))
        text = f"{label} {score}"
        if league:
            text = f"{league}: {text}"
        if isinstance(path, str):
            return f'<a href="{escape(path)}">{text}</a>'
        return text
    if fixture_id is None:
        return ""
    return escape(str(fixture_id))


def _match_list(matches: Any) -> str:
    if not isinstance(matches, list) or not matches:
        return ""
    labels: list[str] = []
    for item in matches:
        if isinstance(item, dict):
            labels.append(str(item.get("label") or item.get("fixture_id") or ""))
    return ", ".join(labels)


def _leg_matches(legs: Any) -> list[dict[str, Any]]:
    if not isinstance(legs, list):
        return []
    out: list[dict[str, Any]] = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        match = leg.get("match")
        if isinstance(match, dict):
            out.append(match)
    return out


_BLOB_KEYS = frozenset(
    {
        "prompt_text",
        "input_json",
        "raw_output",
        "payload",
        "reasons",
        "rationale",
        "notes",
        "candidate_rules",
    }
)


def _is_nested_blob(key: str, value: Any) -> bool:
    return key in _BLOB_KEYS and value is not None


def _cell(value: Any) -> str:
    if isinstance(value, dict) and "path" in value and "label" in value:
        return _match_cell(value, value.get("fixture_id"))
    if isinstance(value, dict):
        return f"<pre>{escape(_pretty(value))}</pre>"
    if isinstance(value, list):
        return f"<pre>{escape(_pretty(value))}</pre>"
    if value is None:
        return ""
    return escape(str(value))


def _pretty(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _table_nav_link(name: str) -> str:
    short = escape(name.removeprefix("advisor_"))
    return f'<a href="/advisor/browse/{escape(name)}">{short}</a>'


def _query(
    filters: dict[str, str], *, before_id: int | None = None, limit: int | None = None
) -> str:
    parts: list[str] = []
    for key, value in filters.items():
        parts.append(f"{key}={value}")
    if before_id is not None:
        parts.append(f"before_id={before_id}")
    if limit is not None:
        parts.append(f"limit={limit}")
    if not parts:
        return ""
    return "?" + "&".join(parts)


def _page(title: str, body: str) -> str:
    links = " · ".join(_table_nav_link(name) for name in TABLE_NAMES)
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <title>{escape(title)}</title>
  <style>
    body {{ margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
      background: #0d1117; color: #e6edf3; }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }}
    a {{ color: #58a6ff; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #30363d;
      vertical-align: top; }}
    th {{ color: #8b949e; font-weight: 600; }}
    pre {{ white-space: pre-wrap; word-break: break-word;
      background: #161b22; padding: 12px; border-radius: 6px; }}
    code {{ background: #161b22; padding: 2px 6px; border-radius: 4px; }}
    .nav {{ color: #8b949e; font-size: 0.9rem; line-height: 1.6; }}
  </style>
</head>
<body><main>
  <p><a href="/advisor/report">Raport</a>
    · <a href="/advisor/recommendations">Rekomendacje</a>
    · <a href="/advisor/strategies">Strategie</a>
    · <a href="/advisor/browse">Tabele</a></p>
  <p class="nav">{links}</p>
  <h1>{escape(title)}</h1>
  {body}
</main></body></html>
"""
