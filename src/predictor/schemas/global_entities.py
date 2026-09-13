"""Pydantic models for W8 global catalog endpoints. Extra fields kept for FR-006."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import AliasChoices, ConfigDict, Field

from predictor.schemas.catalog import ExtraAllow, OptionalDate


class IdName(ExtraAllow):
    id: int | None = None
    name: str | None = None
    logo: str | None = None
    code: str | None = None
    country: str | None = None
    founded: int | None = None
    national: bool | None = None
    winner: bool | None = None
    season: int | None = None


class BirthBlock(ExtraAllow):
    date: OptionalDate = None
    place: str | None = None
    country: str | None = None


class PersonCore(ExtraAllow):
    id: int
    name: str | None = None
    firstname: str | None = None
    lastname: str | None = None
    age: int | None = None
    birth: BirthBlock | None = None
    nationality: str | None = None
    height: str | None = None
    weight: str | None = None
    injured: bool | None = None
    photo: str | None = None


class VenueFull(ExtraAllow):
    id: int | None = None
    name: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None
    capacity: int | None = None
    surface: str | None = None
    image: str | None = None


class TeamEnvelope(ExtraAllow):
    team: IdName
    venue: VenueFull | None = None


class HomeAwayTotal(ExtraAllow):
    home: Any = None
    away: Any = None
    total: Any = None


class FixturesBlock(ExtraAllow):
    played: HomeAwayTotal | None = None
    wins: HomeAwayTotal | None = None
    draws: HomeAwayTotal | None = None
    loses: HomeAwayTotal | None = None


class GoalSide(ExtraAllow):
    total: HomeAwayTotal | None = None
    average: HomeAwayTotal | None = None
    minute: dict[str, Any] | None = None


class GoalsBlock(ExtraAllow):
    for_: GoalSide | None = Field(default=None, validation_alias=AliasChoices("for"))
    against: GoalSide | None = None


class BiggestStreak(ExtraAllow):
    wins: int | None = None
    draws: int | None = None
    loses: int | None = None


class BiggestPair(ExtraAllow):
    home: Any = None
    away: Any = None


class BiggestGoals(ExtraAllow):
    for_: BiggestPair | None = Field(default=None, validation_alias=AliasChoices("for"))
    against: BiggestPair | None = None


class BiggestBlock(ExtraAllow):
    streak: BiggestStreak | None = None
    wins: BiggestPair | None = None
    loses: BiggestPair | None = None
    goals: BiggestGoals | None = None


class PenaltySide(ExtraAllow):
    total: int | None = None
    percentage: str | int | float | None = None


class PenaltyBlock(ExtraAllow):
    scored: PenaltySide | None = None
    missed: PenaltySide | None = None
    total: int | None = None


class CardsBlock(ExtraAllow):
    yellow: dict[str, Any] | None = None
    red: dict[str, Any] | None = None


class TeamStatisticsItem(ExtraAllow):
    league: IdName | None = None
    team: IdName | None = None
    form: str | None = None
    fixtures: FixturesBlock | None = None
    goals: GoalsBlock | None = None
    biggest: BiggestBlock | None = None
    clean_sheet: HomeAwayTotal | None = None
    failed_to_score: HomeAwayTotal | None = None
    penalty: PenaltyBlock | None = None
    lineups: list[Any] = Field(default_factory=list)
    cards: CardsBlock | None = None


class StandingGoals(ExtraAllow):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    for_: int | None = Field(default=None, validation_alias=AliasChoices("for"))
    against: int | None = None


class StandingSide(ExtraAllow):
    played: int | None = None
    win: int | None = None
    draw: int | None = None
    lose: int | None = None
    goals: StandingGoals | None = None


class StandingRow(ExtraAllow):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    rank: int
    team: IdName
    points: int | None = None
    goals_diff: int | None = Field(
        default=None, validation_alias=AliasChoices("goalsDiff", "goals_diff")
    )
    group: str | None = None
    form: str | None = None
    status: str | None = None
    description: str | None = None
    update: datetime | None = None
    all: StandingSide | None = None
    home: StandingSide | None = None
    away: StandingSide | None = None


class StandingLeague(ExtraAllow):
    id: int
    name: str | None = None
    country: str | None = None
    logo: str | None = None
    flag: str | None = None
    season: int | None = None
    standings: list[list[StandingRow]] = Field(default_factory=list)


class StandingItem(ExtraAllow):
    league: StandingLeague


class PlayerGames(ExtraAllow):
    appearences: int | None = None
    lineups: int | None = None
    minutes: int | None = None
    number: int | None = None
    position: str | None = None
    rating: str | int | float | None = None
    captain: bool | None = None


class PlayerSubs(ExtraAllow):
    inn: int | None = Field(default=None, validation_alias=AliasChoices("in"))
    out: int | None = None
    bench: int | None = None


class PairTotalOn(ExtraAllow):
    total: int | None = None
    on: int | None = None


class PlayerGoals(ExtraAllow):
    total: int | None = None
    conceded: int | None = None
    assists: int | None = None
    saves: int | None = None


class PlayerPasses(ExtraAllow):
    total: int | None = None
    key: int | None = None
    accuracy: str | int | float | None = None


class PlayerTackles(ExtraAllow):
    total: int | None = None
    blocks: int | None = None
    interceptions: int | None = None


class PairTotalWon(ExtraAllow):
    total: int | None = None
    won: int | None = None


class PlayerDribbles(ExtraAllow):
    attempts: int | None = None
    success: int | None = None
    past: int | None = None


class PlayerFouls(ExtraAllow):
    drawn: int | None = None
    committed: int | None = None


class PlayerCards(ExtraAllow):
    yellow: int | None = None
    yellowred: int | None = None
    red: int | None = None


class PlayerPenalty(ExtraAllow):
    won: int | None = None
    commited: int | None = None
    scored: int | None = None
    missed: int | None = None
    saved: int | None = None


class PlayerStatLeague(ExtraAllow):
    id: int | None = None
    name: str | None = None
    country: str | None = None
    logo: str | None = None
    flag: str | None = None
    season: int | None = None


class PlayerStatisticsBlock(ExtraAllow):
    team: IdName | None = None
    league: PlayerStatLeague | None = None
    games: PlayerGames | None = None
    substitutes: PlayerSubs | None = None
    shots: PairTotalOn | None = None
    goals: PlayerGoals | None = None
    passes: PlayerPasses | None = None
    tackles: PlayerTackles | None = None
    duels: PairTotalWon | None = None
    dribbles: PlayerDribbles | None = None
    fouls: PlayerFouls | None = None
    cards: PlayerCards | None = None
    penalty: PlayerPenalty | None = None


class PlayerBundle(ExtraAllow):
    player: PersonCore
    statistics: list[PlayerStatisticsBlock] = Field(default_factory=list)


class SquadPlayer(ExtraAllow):
    id: int | None = None
    name: str | None = None
    age: int | None = None
    number: int | None = None
    position: str | None = None
    photo: str | None = None


class SquadItem(ExtraAllow):
    team: IdName | None = None
    players: list[SquadPlayer] = Field(default_factory=list)


class CareerTeamItem(ExtraAllow):
    team: IdName | None = None
    seasons: list[int] = Field(default_factory=list)


class CoachCareerItem(ExtraAllow):
    team: IdName | None = None
    start: str | date | None = None
    end: str | date | None = None


class CoachItem(ExtraAllow):
    id: int
    name: str | None = None
    firstname: str | None = None
    lastname: str | None = None
    age: int | None = None
    birth: BirthBlock | None = None
    nationality: str | None = None
    height: str | None = None
    weight: str | None = None
    photo: str | None = None
    team: IdName | None = None
    career: list[CoachCareerItem] = Field(default_factory=list)


class TransferMoveTeams(ExtraAllow):
    inn: IdName | None = Field(default=None, validation_alias=AliasChoices("in"))
    out: IdName | None = None


class TransferMove(ExtraAllow):
    date: OptionalDate = None
    type: str | None = None
    teams: TransferMoveTeams | None = None


class TransferBundle(ExtraAllow):
    player: PersonCore | IdName | None = None
    update: datetime | None = None
    transfers: list[TransferMove] = Field(default_factory=list)


class TrophyItem(ExtraAllow):
    league: str | None = None
    country: str | None = None
    season: str | int | None = None
    place: str | None = None


class SidelinedItem(ExtraAllow):
    type: str | None = None
    start: str | date | None = None
    end: str | date | None = None
