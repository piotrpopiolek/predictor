"""Pydantic models for /fixtures and /fixtures/rounds. Extra fields kept for FR-006."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import Field

from predictor.schemas.catalog import ExtraAllow


class FixtureVenue(ExtraAllow):
    id: int | None = None
    name: str | None = None
    city: str | None = None


class FixturePeriods(ExtraAllow):
    first: int | None = None
    second: int | None = None


class FixtureStatus(ExtraAllow):
    long: str | None = None
    short: str | None = None
    elapsed: int | None = None
    extra: int | None = None


class FixtureCore(ExtraAllow):
    id: int
    referee: str | None = None
    timezone: str | None = None
    date: datetime | None = None
    timestamp: int | None = None
    periods: FixturePeriods | None = None
    venue: FixtureVenue | None = None
    status: FixtureStatus | None = None


class FixtureLeague(ExtraAllow):
    id: int
    name: str | None = None
    country: str | None = None
    logo: str | None = None
    flag: str | None = None
    season: int | None = None
    round: str | None = None
    standings: bool | None = None
    events: bool | list[Any] | None = None


class FixtureTeam(ExtraAllow):
    id: int
    name: str | None = None
    logo: str | None = None
    winner: bool | None = None


class FixtureTeams(ExtraAllow):
    home: FixtureTeam
    away: FixtureTeam


class ScorePair(ExtraAllow):
    home: int | None = None
    away: int | None = None


class FixtureGoals(ExtraAllow):
    home: int | None = None
    away: int | None = None


class FixtureScore(ExtraAllow):
    halftime: ScorePair | None = None
    fulltime: ScorePair | None = None
    extratime: ScorePair | None = None
    penalty: ScorePair | None = None


class FixtureItem(ExtraAllow):
    fixture: FixtureCore
    league: FixtureLeague
    teams: FixtureTeams
    goals: FixtureGoals | None = None
    score: FixtureScore | None = None
    events: list[Any] | bool | None = None


class RoundDatesItem(ExtraAllow):
    round: str | None = None
    name: str | None = None
    dates: list[date | str] = Field(default_factory=list)
