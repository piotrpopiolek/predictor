"""Pydantic models for /odds and /odds/mapping. Extra fields kept for FR-006."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from predictor.schemas.catalog import ExtraAllow


class OddsValue(ExtraAllow):
    value: str | None = None
    odd: str | int | float | None = None


class OddsBetBlock(ExtraAllow):
    id: int
    name: str | None = None
    values: list[OddsValue] = Field(default_factory=list)


class OddsBookmaker(ExtraAllow):
    id: int
    name: str | None = None
    bets: list[OddsBetBlock] = Field(default_factory=list)


class OddsFixtureStub(ExtraAllow):
    id: int
    timezone: str | None = None
    date: datetime | None = None
    timestamp: int | None = None


class OddsLeagueStub(ExtraAllow):
    id: int | None = None
    name: str | None = None
    season: int | None = None
    country: str | None = None
    logo: str | None = None
    flag: str | None = None


class PrematchOddsItem(ExtraAllow):
    league: OddsLeagueStub | None = None
    fixture: OddsFixtureStub
    update: datetime | None = None
    bookmakers: list[OddsBookmaker] = Field(default_factory=list)


class OddsMappingItem(ExtraAllow):
    league: OddsLeagueStub | None = None
    fixture: OddsFixtureStub
    update: datetime | None = None
