"""Pydantic models for /fixtures?id=, /fixtures/statistics?half=true, /injuries."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import AliasChoices, ConfigDict, Field

from predictor.schemas.catalog import ExtraAllow
from predictor.schemas.fixtures import FixtureItem


class IdName(ExtraAllow):
    id: int | None = None
    name: str | None = None
    photo: str | None = None
    logo: str | None = None
    type: str | None = None
    reason: str | None = None
    update: datetime | None = None
    colors: dict[str, Any] | None = None
    season: int | None = None
    country: str | None = None


class EventTime(ExtraAllow):
    elapsed: int | None = None
    extra: int | None = None


class FixtureEventItem(ExtraAllow):
    time: EventTime | None = None
    team: IdName | None = None
    player: IdName | None = None
    assist: IdName | None = None
    type: str | None = None
    detail: str | None = None
    comments: str | None = None


class LineupPlayerFields(ExtraAllow):
    id: int | None = None
    name: str | None = None
    number: int | None = None
    pos: str | None = None
    grid: str | None = None
    photo: str | None = None


class LineupPlayerWrap(ExtraAllow):
    player: LineupPlayerFields | None = None


class LineupItem(ExtraAllow):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    team: IdName | None = None
    coach: IdName | None = None
    formation: str | None = None
    start_xi: list[LineupPlayerWrap] = Field(default_factory=list, alias="startXI")
    substitutes: list[LineupPlayerWrap] = Field(default_factory=list)


class StatKV(ExtraAllow):
    type: str | None = None
    value: Any = None


class TeamStatisticsItem(ExtraAllow):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    team: IdName | None = None
    statistics: list[StatKV] = Field(default_factory=list)
    statistics_1h: list[StatKV] = Field(
        default_factory=list,
        validation_alias=AliasChoices("statistics_1h", "statistics_1H"),
    )
    statistics_2h: list[StatKV] = Field(
        default_factory=list,
        validation_alias=AliasChoices("statistics_2h", "statistics_2H"),
    )


class PlayerGames(ExtraAllow):
    minutes: int | None = None
    number: int | None = None
    position: str | None = None
    rating: str | int | float | None = None
    captain: bool | None = None
    substitute: bool | None = None


class PairStat(ExtraAllow):
    total: int | None = None
    on: int | None = None
    conceded: int | None = None
    assists: int | None = None
    saves: int | None = None
    key: int | None = None
    accuracy: str | int | float | None = None
    blocks: int | None = None
    interceptions: int | None = None
    won: int | None = None
    attempts: int | None = None
    success: int | None = None
    past: int | None = None
    drawn: int | None = None
    committed: int | None = None
    yellow: int | None = None
    red: int | None = None
    commited: int | None = None
    scored: int | None = None
    missed: int | None = None
    saved: int | None = None


class PlayerStatBlock(ExtraAllow):
    games: PlayerGames | None = None
    offsides: int | None = None
    shots: PairStat | None = None
    goals: PairStat | None = None
    passes: PairStat | None = None
    tackles: PairStat | None = None
    duels: PairStat | None = None
    dribbles: PairStat | None = None
    fouls: PairStat | None = None
    cards: PairStat | None = None
    penalty: PairStat | None = None


class PlayerWithStats(ExtraAllow):
    player: IdName | None = None
    statistics: list[PlayerStatBlock] = Field(default_factory=list)


class TeamPlayersItem(ExtraAllow):
    team: IdName | None = None
    players: list[PlayerWithStats] = Field(default_factory=list)


class FixtureDetail(FixtureItem):
    events: list[FixtureEventItem] = Field(default_factory=list)
    lineups: list[LineupItem] = Field(default_factory=list)
    statistics: list[TeamStatisticsItem] = Field(default_factory=list)
    players: list[TeamPlayersItem] = Field(default_factory=list)


class InjuryItem(ExtraAllow):
    player: IdName | None = None
    team: IdName | None = None
    fixture: IdName | None = None
    league: IdName | None = None
