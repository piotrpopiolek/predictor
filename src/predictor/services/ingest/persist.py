"""Idempotent upserts for W3 dictionary tables."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.models.catalog import (
    Bookmaker,
    Country,
    League,
    LeagueSeason,
    OddsBet,
    OddsLiveBet,
    Season,
)
from predictor.schemas.catalog import (
    CountryItem,
    LeagueResponseItem,
    NamedIdItem,
)

T = TypeVar("T")
_CHUNK = 500


def _chunks(rows: Sequence[T], size: int = _CHUNK) -> list[Sequence[T]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


async def upsert_countries(session: AsyncSession, items: Sequence[CountryItem]) -> int:
    unique: dict[str, CountryItem] = {}
    for item in items:
        name = item.name.strip()
        if not name:
            continue
        unique[name] = item
    if not unique:
        return 0
    count = 0
    for chunk in _chunks(list(unique.values())):
        values = [
            {"name": item.name.strip(), "code": item.code, "flag": item.flag}
            for item in chunk
        ]
        stmt = insert(Country).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Country.name],
            set_={"code": stmt.excluded.code, "flag": stmt.excluded.flag},
        )
        await session.execute(stmt)
        count += len(values)
    return count


async def upsert_seasons(session: AsyncSession, years: Sequence[int]) -> int:
    unique_years = sorted({int(year) for year in years})
    if not unique_years:
        return 0
    count = 0
    for chunk in _chunks(unique_years):
        stmt = insert(Season).values([{"year": year} for year in chunk])
        stmt = stmt.on_conflict_do_nothing(index_elements=[Season.year])
        await session.execute(stmt)
        count += len(chunk)
    return count


async def upsert_named_ids(
    session: AsyncSession,
    model: type[Bookmaker] | type[OddsBet] | type[OddsLiveBet],
    items: Sequence[NamedIdItem],
) -> int:
    unique: dict[int, NamedIdItem] = {item.id: item for item in items}
    if not unique:
        return 0
    count = 0
    for chunk in _chunks(list(unique.values())):
        values = [{"id": item.id, "name": item.name} for item in chunk]
        stmt = insert(model).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[model.id],
            set_={"name": stmt.excluded.name},
        )
        await session.execute(stmt)
        count += len(values)
    return count


def _coverage_flags(item: LeagueResponseItem, season_idx: int) -> dict[str, Any]:
    season = item.seasons[season_idx]
    coverage = season.coverage
    fixtures = coverage.fixtures if coverage is not None else None
    return {
        "cov_events": fixtures.events if fixtures is not None else None,
        "cov_lineups": fixtures.lineups if fixtures is not None else None,
        "cov_statistics_fixtures": (
            fixtures.statistics_fixtures if fixtures is not None else None
        ),
        "cov_statistics_players": (
            fixtures.statistics_players if fixtures is not None else None
        ),
        "cov_standings": coverage.standings if coverage is not None else None,
        "cov_players": coverage.players if coverage is not None else None,
        "cov_top_scorers": coverage.top_scorers if coverage is not None else None,
        "cov_top_assists": coverage.top_assists if coverage is not None else None,
        "cov_top_cards": coverage.top_cards if coverage is not None else None,
        "cov_injuries": coverage.injuries if coverage is not None else None,
        "cov_predictions": coverage.predictions if coverage is not None else None,
        "cov_odds": coverage.odds if coverage is not None else None,
    }


async def upsert_leagues(
    session: AsyncSession, items: Sequence[LeagueResponseItem]
) -> tuple[int, int]:
    nested_countries: list[CountryItem] = []
    nested_years: list[int] = []
    for item in items:
        country = item.country
        if country is not None and country.name and country.name.strip():
            nested_countries.append(
                CountryItem(name=country.name, code=country.code, flag=country.flag)
            )
        for season in item.seasons:
            nested_years.append(season.year)
    await upsert_countries(session, nested_countries)
    await upsert_seasons(session, nested_years)

    unique_leagues: dict[int, LeagueResponseItem] = {item.league.id: item for item in items}
    league_count = 0
    if unique_leagues:
        for chunk in _chunks(list(unique_leagues.values())):
            values = []
            for item in chunk:
                country_name = None
                country_code = None
                if item.country is not None and item.country.name:
                    country_name = item.country.name.strip() or None
                    country_code = item.country.code
                values.append(
                    {
                        "id": item.league.id,
                        "name": item.league.name,
                        "type": item.league.type,
                        "logo": item.league.logo,
                        "country_name": country_name,
                        "country_code": country_code,
                    }
                )
            stmt = insert(League).values(values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[League.id],
                set_={
                    "name": stmt.excluded.name,
                    "type": stmt.excluded.type,
                    "logo": stmt.excluded.logo,
                    "country_name": stmt.excluded.country_name,
                    "country_code": stmt.excluded.country_code,
                },
            )
            await session.execute(stmt)
            league_count += len(values)

    season_rows: dict[tuple[int, int], dict[str, Any]] = {}
    for item in items:
        for index, season in enumerate(item.seasons):
            row = {
                "league_id": item.league.id,
                "season_year": season.year,
                "start_date": season.start,
                "end_date": season.end,
                "is_current": season.current,
                **_coverage_flags(item, index),
            }
            season_rows[(item.league.id, season.year)] = row
    season_count = 0
    if season_rows:
        for chunk in _chunks(list(season_rows.values())):
            stmt = insert(LeagueSeason).values(list(chunk))
            stmt = stmt.on_conflict_do_update(
                index_elements=[LeagueSeason.league_id, LeagueSeason.season_year],
                set_={
                    "start_date": stmt.excluded.start_date,
                    "end_date": stmt.excluded.end_date,
                    "is_current": stmt.excluded.is_current,
                    "cov_events": stmt.excluded.cov_events,
                    "cov_lineups": stmt.excluded.cov_lineups,
                    "cov_statistics_fixtures": stmt.excluded.cov_statistics_fixtures,
                    "cov_statistics_players": stmt.excluded.cov_statistics_players,
                    "cov_standings": stmt.excluded.cov_standings,
                    "cov_players": stmt.excluded.cov_players,
                    "cov_top_scorers": stmt.excluded.cov_top_scorers,
                    "cov_top_assists": stmt.excluded.cov_top_assists,
                    "cov_top_cards": stmt.excluded.cov_top_cards,
                    "cov_injuries": stmt.excluded.cov_injuries,
                    "cov_predictions": stmt.excluded.cov_predictions,
                    "cov_odds": stmt.excluded.cov_odds,
                },
            )
            await session.execute(stmt)
            season_count += len(chunk)
    return league_count, season_count
