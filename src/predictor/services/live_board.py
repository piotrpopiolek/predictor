"""Read-only live match board for the status process. Never calls API-Football."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from itertools import groupby
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import aliased

from predictor.models.catalog import League
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture, Team

REFRESH_SECONDS = 15


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
        }


def clock_label(status_short: str, elapsed: int | None, extra: int | None) -> str:
    if status_short == "HT":
        return "HT"
    if elapsed is None:
        return status_short
    if extra:
        return f"{elapsed}+{extra}'"
    return f"{elapsed}'"


def safe_http_url(url: str | None) -> str | None:
    if url is None:
        return None
    stripped = url.strip()
    if stripped.startswith("https://") or stripped.startswith("http://"):
        return stripped
    return None


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


def parse_live_fixture_ids(params: dict[str, Any] | None) -> list[int]:
    """Ids from the last `/fixtures?live=all` task. status_short stays 2H after FT."""
    if not params:
        return []
    raw = params.get("fixture_ids")
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


async def load_live_all_fixture_ids(session: AsyncSession) -> list[int]:
    task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.fixture_id.is_(None))
        .where(EtlTask.day_utc.is_(None))
        .where(EtlTask.params.contains({"live": "all"}))
        .order_by(EtlTask.id)
        .limit(1)
    )
    if task is None:
        return []
    return parse_live_fixture_ids(task.params)


async def list_live_matches(engine: AsyncEngine) -> list[LiveMatch]:
    home = aliased(Team)
    away = aliased(Team)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        live_ids = await load_live_all_fixture_ids(session)
        if not live_ids:
            return []
        stmt = (
            select(
                Fixture.id,
                League.country_name,
                League.name,
                Fixture.round,
                home.name,
                away.name,
                home.logo,
                away.logo,
                Fixture.goals_home,
                Fixture.goals_away,
                Fixture.status_short,
                Fixture.status_long,
                Fixture.elapsed_minutes,
                Fixture.extra_minutes,
            )
            .join(League, League.id == Fixture.league_id)
            .join(home, home.id == Fixture.home_team_id)
            .join(away, away.id == Fixture.away_team_id)
            .where(Fixture.id.in_(live_ids))
            .order_by(
                League.country_name.asc().nulls_last(),
                League.name.asc().nulls_last(),
                Fixture.elapsed_minutes.desc().nulls_last(),
                Fixture.id,
            )
        )
        rows = (await session.execute(stmt)).all()
    matches: list[LiveMatch] = []
    for row in rows:
        status = str(row[10] or "LIVE")
        matches.append(
            LiveMatch(
                fixture_id=int(row[0]),
                country=_name(row[1]),
                league=_name(row[2]),
                round=row[3],
                home=_name(row[4]),
                away=_name(row[5]),
                home_logo=safe_http_url(row[6]),
                away_logo=safe_http_url(row[7]),
                goals_home=row[8],
                goals_away=row[9],
                status_short=status,
                status_long=row[11],
                elapsed=row[12],
                extra=row[13],
            )
        )
    return matches


def _img(url: str | None, label: str) -> str:
    if url is None:
        return '<span class="logo fallback" aria-hidden="true"></span>'
    src = escape(url, quote=True)
    alt = escape(label, quote=True)
    return (
        f'<img class="logo" src="{src}" alt="{alt}" width="28" height="28" '
        'loading="lazy" referrerpolicy="no-referrer">'
    )


def _score(value: int | None) -> str:
    return "—" if value is None else str(value)


def _match_card(match: LiveMatch) -> str:
    home = escape(match.home)
    away = escape(match.away)
    clock = escape(match.clock)
    status = escape(match.status_short)
    round_html = ""
    if match.round:
        round_html = f'<span class="round">{escape(match.round)}</span>'
    return f"""
<article class="match">
  <div class="team home">
    {_img(match.home_logo, match.home)}
    <span class="name">{home}</span>
  </div>
  <div class="scoreblock">
    <div class="score">{_score(match.goals_home)}–{_score(match.goals_away)}</div>
    <div class="meta"><span class="clock">{clock}</span>
      <span class="status">{status}</span></div>
    {round_html}
  </div>
  <div class="team away">
    {_img(match.away_logo, match.away)}
    <span class="name">{away}</span>
  </div>
</article>
"""


def render_live_html(matches: list[LiveMatch], *, generated_at: datetime) -> str:
    count = len(matches)
    sections: list[str] = []
    for (country, league), rows in groupby(
        matches, key=lambda item: (item.country, item.league)
    ):
        cards = "".join(_match_card(item) for item in rows)
        sections.append(f"""
<section class="league">
  <h2>{escape(country)} · {escape(league)}</h2>
  {cards}
</section>
""")
    body = "".join(sections)
    if not body:
        body = '<p class="empty">Żaden mecz nie jest teraz w grze.</p>'
    when = escape(generated_at.strftime("%H:%M:%S UTC"))
    noun = _match_noun(count)
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>Mecze na żywo</title>
<style>
  :root {{
    --bg: #0d1117;
    --card: #161b22;
    --line: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --live: #3fb950;
    --score: #f0f6fc;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.4;
  }}
  header {{
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
  }}
  h1 {{
    margin: 0;
    font-size: 1.25rem;
    font-weight: 650;
    display: flex;
    gap: 10px;
    align-items: center;
  }}
  .dot {{
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--live);
    box-shadow: 0 0 0 4px rgba(63, 185, 80, 0.25);
  }}
  .sub {{ color: var(--muted); font-size: 0.9rem; }}
  main {{ max-width: 920px; margin: 0 auto; padding: 16px 20px 40px; }}
  .league {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    margin-bottom: 16px;
    overflow: hidden;
  }}
  h2 {{
    margin: 0;
    padding: 12px 16px;
    font-size: 0.85rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--muted);
    border-bottom: 1px solid var(--line);
  }}
  .match {{
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 12px;
    align-items: center;
    padding: 14px 16px;
    border-bottom: 1px solid var(--line);
  }}
  .match:last-child {{ border-bottom: 0; }}
  .team {{
    display: flex;
    gap: 10px;
    align-items: center;
    min-width: 0;
  }}
  .team.away {{ flex-direction: row-reverse; text-align: right; }}
  .name {{
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .logo {{
    width: 28px;
    height: 28px;
    object-fit: contain;
    flex-shrink: 0;
    border-radius: 4px;
    background: #0d1117;
  }}
  .logo.fallback {{
    display: inline-block;
    border: 1px solid var(--line);
  }}
  .scoreblock {{ text-align: center; min-width: 7.5rem; }}
  .score {{
    font-variant-numeric: tabular-nums;
    font-size: 1.45rem;
    font-weight: 700;
    color: var(--score);
    letter-spacing: 0.04em;
  }}
  .meta {{
    color: var(--muted);
    font-size: 0.8rem;
    display: flex;
    gap: 8px;
    justify-content: center;
  }}
  .clock {{ color: var(--live); font-weight: 650; }}
  .round {{ display: block; margin-top: 4px; color: var(--muted); font-size: 0.75rem; }}
  .empty {{ color: var(--muted); text-align: center; padding: 48px 16px; }}
  @media (max-width: 640px) {{
    .match {{ grid-template-columns: 1fr; text-align: center; }}
    .team, .team.away {{ flex-direction: column; }}
    .name {{ white-space: normal; }}
  }}
</style>
</head>
<body>
<header>
  <h1><span class="dot" aria-hidden="true"></span> Mecze na żywo</h1>
  <p class="sub">{count} {noun} · odświeżanie co {REFRESH_SECONDS} s · {when}</p>
</header>
<main>
{body}
</main>
</body>
</html>
"""
