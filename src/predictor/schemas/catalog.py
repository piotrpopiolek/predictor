"""Pydantic models for W3 dictionary endpoints. Extra fields are kept for drift alerts."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _empty_to_none(value: Any) -> Any:
    if value == "":
        return None
    return value


OptionalDate = Annotated[date | None, BeforeValidator(_empty_to_none)]


class ExtraAllow(BaseModel):
    model_config = ConfigDict(extra="allow")


class CountryItem(ExtraAllow):
    name: str
    code: str | None = None
    flag: str | None = None


class NamedIdItem(ExtraAllow):
    id: int
    name: str | None = None


class LeagueCore(ExtraAllow):
    id: int
    name: str | None = None
    type: str | None = None
    logo: str | None = None


class LeagueCountry(ExtraAllow):
    name: str | None = None
    code: str | None = None
    flag: str | None = None


class CoverageFixtures(ExtraAllow):
    events: bool | None = None
    lineups: bool | None = None
    statistics_fixtures: bool | None = None
    statistics_players: bool | None = None


class LeagueCoverage(ExtraAllow):
    fixtures: CoverageFixtures | None = None
    standings: bool | None = None
    players: bool | None = None
    top_scorers: bool | None = None
    top_assists: bool | None = None
    top_cards: bool | None = None
    injuries: bool | None = None
    predictions: bool | None = None
    odds: bool | None = None


class LeagueSeasonItem(ExtraAllow):
    year: int
    start: OptionalDate = None
    end: OptionalDate = None
    current: bool | None = None
    coverage: LeagueCoverage | None = None


class LeagueResponseItem(ExtraAllow):
    league: LeagueCore
    country: LeagueCountry | None = None
    seasons: list[LeagueSeasonItem] = Field(default_factory=list)
