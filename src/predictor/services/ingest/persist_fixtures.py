"""Idempotent upserts for W4 fixtures, nested teams/venues, and rounds."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.models.catalog import League, LeagueSeason
from predictor.models.fixtures import (
    Fixture,
    FixtureStatusRow,
    LeagueRound,
    Team,
    Venue,
)
from predictor.schemas.catalog import CountryItem
from predictor.schemas.fixtures import FixtureItem, FixtureTeam, FixtureVenue
from predictor.services.ingest.persist import _chunks, upsert_countries, upsert_seasons


async def _known_status_codes(session: AsyncSession) -> set[str]:
    rows = await session.scalars(select(FixtureStatusRow.code))
    return set(rows)


def _pair(score: Any) -> tuple[int | None, int | None]:
    if score is None:
        return None, None
    return score.home, score.away


async def _upsert_venues(session: AsyncSession, items: Sequence[FixtureVenue]) -> int:
    unique: dict[int, FixtureVenue] = {}
    for item in items:
        if item.id is None:
            continue
        unique[item.id] = item
    if not unique:
        return 0
    count = 0
    for chunk in _chunks(list(unique.values())):
        values = [
            {"id": item.id, "name": item.name, "city": item.city} for item in chunk
        ]
        stmt = insert(Venue).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Venue.id],
            set_={
                "name": func.coalesce(stmt.excluded.name, Venue.name),
                "city": func.coalesce(stmt.excluded.city, Venue.city),
            },
        )
        await session.execute(stmt)
        count += len(values)
    return count


async def upsert_teams(session: AsyncSession, items: Sequence[FixtureTeam]) -> int:
    unique: dict[int, FixtureTeam] = {item.id: item for item in items}
    if not unique:
        return 0
    count = 0
    for chunk in _chunks(list(unique.values())):
        values = [
            {"id": item.id, "name": item.name, "logo": item.logo} for item in chunk
        ]
        stmt = insert(Team).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Team.id],
            set_={
                "name": func.coalesce(stmt.excluded.name, Team.name),
                "logo": func.coalesce(stmt.excluded.logo, Team.logo),
            },
        )
        await session.execute(stmt)
        count += len(values)
    return count


async def _upsert_nested_leagues(
    session: AsyncSession, items: Sequence[FixtureItem]
) -> None:
    countries: list[CountryItem] = []
    years: list[int] = []
    unique_leagues: dict[int, FixtureItem] = {}
    season_keys: set[tuple[int, int]] = set()
    for item in items:
        unique_leagues[item.league.id] = item
        if item.league.country and item.league.country.strip():
            countries.append(CountryItem(name=item.league.country.strip()))
        if item.league.season is not None:
            years.append(item.league.season)
            season_keys.add((item.league.id, item.league.season))
    await upsert_countries(session, countries)
    await upsert_seasons(session, years)
    if unique_leagues:
        values = []
        for item in unique_leagues.values():
            country_name = None
            if item.league.country and item.league.country.strip():
                country_name = item.league.country.strip()
            values.append(
                {
                    "id": item.league.id,
                    "name": item.league.name,
                    "logo": item.league.logo,
                    "country_name": country_name,
                }
            )
        stmt = insert(League).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[League.id],
            set_={
                "name": func.coalesce(stmt.excluded.name, League.name),
                "logo": func.coalesce(stmt.excluded.logo, League.logo),
                "country_name": func.coalesce(
                    stmt.excluded.country_name, League.country_name
                ),
            },
        )
        await session.execute(stmt)
    if season_keys:
        season_values = [
            {"league_id": league_id, "season_year": year}
            for league_id, year in season_keys
        ]
        stmt = insert(LeagueSeason).values(season_values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[LeagueSeason.league_id, LeagueSeason.season_year]
        )
        await session.execute(stmt)


def _fixture_row(
    item: FixtureItem, *, now: datetime, valid_statuses: set[str]
) -> dict[str, Any] | None:
    if item.league.season is None:
        return None
    short = item.fixture.status.short if item.fixture.status is not None else None
    if short is not None and short not in valid_statuses:
        return None
    venue = item.fixture.venue
    periods = item.fixture.periods
    goals = item.goals
    score = item.score
    ht_home, ht_away = _pair(score.halftime if score is not None else None)
    ft_home, ft_away = _pair(score.fulltime if score is not None else None)
    et_home, et_away = _pair(score.extratime if score is not None else None)
    pn_home, pn_away = _pair(score.penalty if score is not None else None)
    return {
        "id": item.fixture.id,
        "referee": item.fixture.referee,
        "timezone": item.fixture.timezone,
        "date": item.fixture.date,
        "timestamp_utc": item.fixture.timestamp,
        "period_first": periods.first if periods is not None else None,
        "period_second": periods.second if periods is not None else None,
        "venue_id": venue.id if venue is not None else None,
        "venue_name": venue.name if venue is not None else None,
        "venue_city": venue.city if venue is not None else None,
        "status_short": short,
        "status_long": (
            item.fixture.status.long if item.fixture.status is not None else None
        ),
        "elapsed_minutes": (
            item.fixture.status.elapsed if item.fixture.status is not None else None
        ),
        "extra_minutes": (
            item.fixture.status.extra if item.fixture.status is not None else None
        ),
        "league_id": item.league.id,
        "season": item.league.season,
        "round": item.league.round,
        "home_team_id": item.teams.home.id,
        "away_team_id": item.teams.away.id,
        "home_winner": item.teams.home.winner,
        "away_winner": item.teams.away.winner,
        "goals_home": goals.home if goals is not None else None,
        "goals_away": goals.away if goals is not None else None,
        "goals_home_halftime": ht_home,
        "goals_away_halftime": ht_away,
        "goals_home_fulltime": ft_home,
        "goals_away_fulltime": ft_away,
        "goals_home_extratime": et_home,
        "goals_away_extratime": et_away,
        "goals_home_penalty": pn_home,
        "goals_away_penalty": pn_away,
        "synced_at": now,
    }


async def upsert_fixtures(
    session: AsyncSession, items: Sequence[FixtureItem]
) -> dict[str, Any]:
    valid_statuses = await _known_status_codes(session)
    now = datetime.now(UTC)
    skipped = 0
    ready: list[FixtureItem] = []
    for item in items:
        row = _fixture_row(item, now=now, valid_statuses=valid_statuses)
        if row is None:
            skipped += 1
            continue
        ready.append(item)
    await _upsert_nested_leagues(session, ready)
    venues = [item.fixture.venue for item in ready if item.fixture.venue is not None]
    await _upsert_venues(session, venues)
    teams: list[FixtureTeam] = []
    for item in ready:
        teams.append(item.teams.home)
        teams.append(item.teams.away)
    await upsert_teams(session, teams)

    unique: dict[int, dict[str, Any]] = {}
    for item in ready:
        row = _fixture_row(item, now=now, valid_statuses=valid_statuses)
        if row is not None:
            unique[item.fixture.id] = row
    if unique:
        for chunk in _chunks(list(unique.values())):
            values = list(chunk)
            stmt = insert(Fixture).values(values)
            update_cols = {
                key: getattr(stmt.excluded, key) for key in values[0] if key != "id"
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=[Fixture.id],
                set_=update_cols,
            )
            await session.execute(stmt)
    pairs = sorted(
        {
            (item.league.id, item.league.season)
            for item in ready
            if item.league.season is not None
        }
    )
    discovered = [
        {
            "id": fixture_id,
            "home": row["home_team_id"],
            "away": row["away_team_id"],
            "league": row["league_id"],
            "season": row["season"],
        }
        for fixture_id, row in unique.items()
    ]
    return {
        "count": len(unique),
        "skipped": skipped,
        "league_seasons": [
            {"league": league_id, "season": season} for league_id, season in pairs
        ],
        "fixture_ids": sorted(unique),
        "discovered": discovered,
    }


def _round_dates(raw: list[date | str]) -> list[date] | None:
    parsed: list[date] = []
    for value in raw:
        if isinstance(value, datetime):
            parsed.append(value.date())
            continue
        if isinstance(value, date):
            parsed.append(value)
            continue
        text = str(value)[:10]
        try:
            parsed.append(date.fromisoformat(text))
        except ValueError:
            continue
    return parsed or None


async def upsert_league_rounds(
    session: AsyncSession,
    league_id: int,
    season: int,
    items: Sequence[object],
) -> int:
    rows: dict[str, dict[str, Any]] = {}
    for item in items:
        name: str | None
        dates: list[date] | None = None
        if isinstance(item, str):
            name = item.strip() or None
        elif isinstance(item, dict):
            raw_name = item.get("round") or item.get("name")
            name = str(raw_name).strip() if raw_name else None
            raw_dates = item.get("dates") or []
            if isinstance(raw_dates, list):
                dates = _round_dates(raw_dates)
        else:
            continue
        if not name:
            continue
        rows[name] = {
            "league_id": league_id,
            "season": season,
            "round_name": name,
            "dates": dates,
        }
    if not rows:
        return 0
    count = 0
    for chunk in _chunks(list(rows.values())):
        stmt = insert(LeagueRound).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                LeagueRound.league_id,
                LeagueRound.season,
                LeagueRound.round_name,
            ],
            set_={"dates": stmt.excluded.dates},
        )
        await session.execute(stmt)
        count += len(chunk)
    return count
