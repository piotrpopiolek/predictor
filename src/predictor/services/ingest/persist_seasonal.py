"""Shared helpers and persist for teams, venues, standings, team season stats."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.constants import TEAM_STATS_SENTINEL_DATE
from predictor.models.fixtures import Team, Venue
from predictor.models.seasonal import Standing, TeamSeasonStatistics
from predictor.schemas.catalog import CountryItem
from predictor.schemas.fixtures import FixtureTeam
from predictor.schemas.global_entities import (
    IdName,
    StandingItem,
    StandingSide,
    TeamEnvelope,
    TeamStatisticsItem,
    VenueFull,
)
from predictor.services.ingest.persist import _chunks, upsert_countries, upsert_seasons
from predictor.services.ingest.persist_fixtures import upsert_teams


def as_str(raw: object | None, limit: int) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.casefold() in {"unknown", "n/a", "null"}:
        return None
    return text[:limit]


def as_int(raw: object | None) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def parse_flexible_date(raw: object | None) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text or text.casefold() in {"unknown", "n/a", "null"}:
        return None
    if len(text) == 4 and text.isdigit():
        return date(int(text), 1, 1)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def sentinel_date() -> date:
    return date.fromisoformat(TEAM_STATS_SENTINEL_DATE)


async def upsert_team_ids(session: AsyncSession, teams: Sequence[IdName]) -> None:
    ready = [
        FixtureTeam(id=item.id, name=item.name, logo=item.logo)
        for item in teams
        if item.id is not None
    ]
    if ready:
        await upsert_teams(session, ready)


async def upsert_venues_full(
    session: AsyncSession, items: Sequence[VenueFull]
) -> int:
    countries: list[CountryItem] = []
    unique: dict[int, VenueFull] = {}
    for item in items:
        if item.id is None:
            continue
        unique[item.id] = item
        if item.country and item.country.strip():
            countries.append(CountryItem(name=item.country.strip()))
    await upsert_countries(session, countries)
    if not unique:
        return 0
    count = 0
    for chunk in _chunks(list(unique.values())):
        values = [
            {
                "id": item.id,
                "name": item.name,
                "address": item.address,
                "city": item.city,
                "country_name": item.country.strip() if item.country else None,
                "capacity": item.capacity,
                "surface": item.surface,
                "image": item.image,
            }
            for item in chunk
        ]
        stmt = insert(Venue).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Venue.id],
            set_={
                "name": func.coalesce(stmt.excluded.name, Venue.name),
                "address": func.coalesce(stmt.excluded.address, Venue.address),
                "city": func.coalesce(stmt.excluded.city, Venue.city),
                "country_name": func.coalesce(
                    stmt.excluded.country_name, Venue.country_name
                ),
                "capacity": func.coalesce(stmt.excluded.capacity, Venue.capacity),
                "surface": func.coalesce(stmt.excluded.surface, Venue.surface),
                "image": func.coalesce(stmt.excluded.image, Venue.image),
            },
        )
        await session.execute(stmt)
        count += len(values)
    return count


async def persist_teams(
    session: AsyncSession, items: Sequence[TeamEnvelope]
) -> dict[str, Any]:
    venues = [item.venue for item in items if item.venue is not None]
    venue_count = await upsert_venues_full(session, venues)
    countries: list[CountryItem] = []
    values: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in items:
        team = item.team
        if team.id is None or team.id in seen:
            continue
        seen.add(team.id)
        country = team.country.strip() if team.country else None
        if country:
            countries.append(CountryItem(name=country))
        venue_id = item.venue.id if item.venue is not None else None
        values.append(
            {
                "id": team.id,
                "name": team.name,
                "code": as_str(team.code, 10),
                "country_name": country,
                "founded": team.founded,
                "national": team.national,
                "logo": team.logo,
                "venue_id": venue_id,
            }
        )
    await upsert_countries(session, countries)
    venue_ids = sorted(
        {
            item.venue.id
            for item in items
            if item.venue is not None and item.venue.id is not None
        }
    )
    if not values:
        return {"teams": 0, "venues": venue_count, "venue_ids": venue_ids}
    for chunk in _chunks(values):
        stmt = insert(Team).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=[Team.id],
            set_={
                "name": func.coalesce(stmt.excluded.name, Team.name),
                "code": func.coalesce(stmt.excluded.code, Team.code),
                "country_name": func.coalesce(
                    stmt.excluded.country_name, Team.country_name
                ),
                "founded": func.coalesce(stmt.excluded.founded, Team.founded),
                "national": func.coalesce(stmt.excluded.national, Team.national),
                "logo": func.coalesce(stmt.excluded.logo, Team.logo),
                "venue_id": func.coalesce(stmt.excluded.venue_id, Team.venue_id),
            },
        )
        await session.execute(stmt)
    return {"teams": len(values), "venues": venue_count, "venue_ids": venue_ids}


def _side_goals(side: StandingSide | None) -> tuple[int | None, int | None]:
    if side is None or side.goals is None:
        return None, None
    return side.goals.for_, side.goals.against


async def persist_standings(
    session: AsyncSession, items: Sequence[StandingItem]
) -> dict[str, Any]:
    teams: list[IdName] = []
    years: list[int] = []
    rows: dict[tuple[int, int, str, int], dict[str, Any]] = {}
    keys: set[tuple[int, int]] = set()
    for item in items:
        league = item.league
        if league.season is None:
            continue
        years.append(league.season)
        keys.add((league.id, league.season))
        for group in league.standings:
            for row in group:
                if row.team.id is None:
                    continue
                teams.append(row.team)
                group_name = (row.group or "").strip()
                all_side = row.all
                home = row.home
                away = row.away
                gf, ga = _side_goals(all_side)
                home_gf, home_ga = _side_goals(home)
                away_gf, away_ga = _side_goals(away)
                rows[(league.id, league.season, group_name, row.team.id)] = {
                    "league_id": league.id,
                    "season": league.season,
                    "group_name": group_name,
                    "rank": row.rank,
                    "team_id": row.team.id,
                    "points": row.points,
                    "goals_diff": row.goals_diff,
                    "form": as_str(row.form, 50),
                    "status": as_str(row.status, 16),
                    "description": row.description,
                    "api_update": row.update,
                    "played": all_side.played if all_side else None,
                    "win": all_side.win if all_side else None,
                    "draw": all_side.draw if all_side else None,
                    "lose": all_side.lose if all_side else None,
                    "goals_for": gf,
                    "goals_against": ga,
                    "home_played": home.played if home else None,
                    "home_win": home.win if home else None,
                    "home_draw": home.draw if home else None,
                    "home_lose": home.lose if home else None,
                    "home_gf": home_gf,
                    "home_ga": home_ga,
                    "away_played": away.played if away else None,
                    "away_win": away.win if away else None,
                    "away_draw": away.draw if away else None,
                    "away_lose": away.lose if away else None,
                    "away_gf": away_gf,
                    "away_ga": away_ga,
                }
    await upsert_seasons(session, years)
    await upsert_team_ids(session, teams)
    for league_id, season in keys:
        await session.execute(
            delete(Standing).where(
                Standing.league_id == league_id, Standing.season == season
            )
        )
    stored = 0
    if rows:
        for chunk in _chunks(list(rows.values())):
            await session.execute(insert(Standing).values(list(chunk)))
            stored += len(chunk)
    return {
        "count": stored,
        "team_ids": sorted({row["team_id"] for row in rows.values()}),
        "league_seasons": [
            {"league": league_id, "season": season}
            for league_id, season in sorted(keys)
        ],
    }


def _hat(block: Any, field: str, *, as_text: bool = False) -> Any:
    if block is None:
        return None
    raw = getattr(block, field, None)
    return as_str(raw, 16) if as_text else as_int(raw)


async def persist_team_statistics(
    session: AsyncSession,
    items: Sequence[TeamStatisticsItem],
    *,
    as_of: date,
    fallback_team: int | None = None,
    fallback_league: int | None = None,
    fallback_season: int | None = None,
) -> int:
    stored = 0
    for item in items:
        team_id = item.team.id if item.team is not None else fallback_team
        league_id = item.league.id if item.league is not None else fallback_league
        season = item.league.season if item.league is not None else None
        if season is None:
            season = fallback_season
        if team_id is None or league_id is None or season is None:
            continue
        await upsert_team_ids(
            session,
            [IdName(id=team_id, name=item.team.name if item.team else None)],
        )
        fx = item.fixtures
        goals = item.goals
        gf = goals.for_ if goals is not None else None
        ga = goals.against if goals is not None else None
        biggest = item.biggest
        streak = biggest.streak if biggest is not None else None
        wins = biggest.wins if biggest is not None else None
        loses = biggest.loses if biggest is not None else None
        bgoals = biggest.goals if biggest is not None else None
        bfor = bgoals.for_ if bgoals is not None else None
        bagainst = bgoals.against if bgoals is not None else None
        pen = item.penalty
        scored = pen.scored if pen is not None else None
        missed = pen.missed if pen is not None else None
        cards = item.cards
        values = {
            "team_id": team_id,
            "league_id": league_id,
            "season": season,
            "as_of_date": as_of,
            "form": item.form,
            "played_home": _hat(fx.played if fx else None, "home"),
            "played_away": _hat(fx.played if fx else None, "away"),
            "played_total": _hat(fx.played if fx else None, "total"),
            "wins_home": _hat(fx.wins if fx else None, "home"),
            "wins_away": _hat(fx.wins if fx else None, "away"),
            "wins_total": _hat(fx.wins if fx else None, "total"),
            "draws_home": _hat(fx.draws if fx else None, "home"),
            "draws_away": _hat(fx.draws if fx else None, "away"),
            "draws_total": _hat(fx.draws if fx else None, "total"),
            "loses_home": _hat(fx.loses if fx else None, "home"),
            "loses_away": _hat(fx.loses if fx else None, "away"),
            "loses_total": _hat(fx.loses if fx else None, "total"),
            "gf_home": _hat(gf.total if gf else None, "home"),
            "gf_away": _hat(gf.total if gf else None, "away"),
            "gf_total": _hat(gf.total if gf else None, "total"),
            "ga_home": _hat(ga.total if ga else None, "home"),
            "ga_away": _hat(ga.total if ga else None, "away"),
            "ga_total": _hat(ga.total if ga else None, "total"),
            "gf_avg_home": _hat(gf.average if gf else None, "home", as_text=True),
            "gf_avg_away": _hat(gf.average if gf else None, "away", as_text=True),
            "gf_avg_total": _hat(gf.average if gf else None, "total", as_text=True),
            "ga_avg_home": _hat(ga.average if ga else None, "home", as_text=True),
            "ga_avg_away": _hat(ga.average if ga else None, "away", as_text=True),
            "ga_avg_total": _hat(ga.average if ga else None, "total", as_text=True),
            "gf_minute": gf.minute if gf is not None else None,
            "ga_minute": ga.minute if ga is not None else None,
            "streak_wins": streak.wins if streak else None,
            "streak_draws": streak.draws if streak else None,
            "streak_loses": streak.loses if streak else None,
            "biggest_win_home": as_str(wins.home if wins else None, 16),
            "biggest_win_away": as_str(wins.away if wins else None, 16),
            "biggest_loss_home": as_str(loses.home if loses else None, 16),
            "biggest_loss_away": as_str(loses.away if loses else None, 16),
            "biggest_gf_home": as_int(bfor.home if bfor else None),
            "biggest_gf_away": as_int(bfor.away if bfor else None),
            "biggest_ga_home": as_int(bagainst.home if bagainst else None),
            "biggest_ga_away": as_int(bagainst.away if bagainst else None),
            "cs_home": _hat(item.clean_sheet, "home"),
            "cs_away": _hat(item.clean_sheet, "away"),
            "cs_total": _hat(item.clean_sheet, "total"),
            "fts_home": _hat(item.failed_to_score, "home"),
            "fts_away": _hat(item.failed_to_score, "away"),
            "fts_total": _hat(item.failed_to_score, "total"),
            "pen_scored": scored.total if scored else None,
            "pen_scored_pct": as_str(scored.percentage if scored else None, 16),
            "pen_missed": missed.total if missed else None,
            "pen_missed_pct": as_str(missed.percentage if missed else None, 16),
            "pen_total": pen.total if pen else None,
            "lineups": item.lineups or None,
            "cards_yellow": cards.yellow if cards else None,
            "cards_red": cards.red if cards else None,
        }
        stmt = insert(TeamSeasonStatistics).values(values)
        update_cols = {
            key: getattr(stmt.excluded, key)
            for key in values
            if key not in {"team_id", "league_id", "season", "as_of_date"}
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                TeamSeasonStatistics.team_id,
                TeamSeasonStatistics.league_id,
                TeamSeasonStatistics.season,
                TeamSeasonStatistics.as_of_date,
            ],
            set_=update_cols,
        )
        await session.execute(stmt)
        stored += 1
    return stored
