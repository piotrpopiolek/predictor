"""Pydantic models for /odds/live. Extra fields kept for FR-006."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from predictor.schemas.catalog import ExtraAllow, OddLabel


class LiveFixtureStatus(ExtraAllow):
    long: str | None = None
    elapsed: int | None = None
    seconds: str | None = None
    short: str | None = None


class LiveFixtureStub(ExtraAllow):
    id: int
    status: LiveFixtureStatus | None = None


class LiveLeague(ExtraAllow):
    id: int
    season: int | None = None


class LiveTeamSide(ExtraAllow):
    id: int | None = None
    goals: int | None = None


class LiveTeams(ExtraAllow):
    home: LiveTeamSide | None = None
    away: LiveTeamSide | None = None


class LiveMatchFlags(ExtraAllow):
    stopped: bool | None = None
    blocked: bool | None = None
    finished: bool | None = None


class LiveOddValue(ExtraAllow):
    value: OddLabel = None
    odd: str | int | float | None = None
    handicap: str | int | float | None = None
    main: bool | None = None
    suspended: bool | None = None


class LiveOddBet(ExtraAllow):
    id: int
    name: str | None = None
    values: list[LiveOddValue] = Field(default_factory=list)


class OddsLiveItem(ExtraAllow):
    fixture: LiveFixtureStub
    league: LiveLeague | None = None
    teams: LiveTeams | None = None
    status: LiveMatchFlags | None = None
    update: datetime | None = None
    odds: list[LiveOddBet] = Field(default_factory=list)
