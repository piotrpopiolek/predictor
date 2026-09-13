"""ORM for seasonal aggregates (standings, player stats, squads, team season)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from predictor.models.base import Base


class Standing(Base):
    __tablename__ = "standings"
    __table_args__ = (UniqueConstraint("league_id", "season", "group_name", "team_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    league_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("leagues.id"), nullable=False
    )
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    group_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id"), nullable=False
    )
    points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_diff: Mapped[int | None] = mapped_column(Integer, nullable=True)
    form: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_update: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    played: Mapped[int | None] = mapped_column(Integer, nullable=True)
    win: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draw: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lose: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_for: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_against: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_played: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_win: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_draw: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_lose: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_gf: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_ga: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_played: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_win: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_draw: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_lose: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_gf: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_ga: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PlayerStatistic(Base):
    __tablename__ = "player_statistics"
    __table_args__ = (UniqueConstraint("player_id", "team_id", "league_id", "season"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("players.id"), nullable=False
    )
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id"), nullable=False
    )
    league_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("leagues.id"), nullable=False
    )
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    appearences: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lineups: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rating: Mapped[str | None] = mapped_column(String(8), nullable=True)
    captain: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sub_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sub_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sub_bench: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shots_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shots_on: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_conceded: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assists: Mapped[int | None] = mapped_column(Integer, nullable=True)
    saves: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passes_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passes_key: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passes_accuracy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tackles: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blocks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interceptions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duels_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duels_won: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dribbles_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dribbles_success: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dribbles_past: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fouls_drawn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fouls_committed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yellow: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yellowred: Mapped[int | None] = mapped_column(Integer, nullable=True)
    red: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pen_won: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pen_committed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pen_scored: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pen_missed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pen_saved: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SquadMember(Base):
    __tablename__ = "squad_members"

    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id"), primary_key=True
    )
    player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("players.id"), primary_key=True
    )
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[str | None] = mapped_column(String(32), nullable=True)


class PlayerCareerTeam(Base):
    __tablename__ = "player_career_teams"

    player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("players.id"), primary_key=True
    )
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id"), primary_key=True
    )
    season: Mapped[int] = mapped_column(Integer, primary_key=True)


class TeamSeasonStatistics(Base):
    __tablename__ = "team_season_statistics"

    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id"), primary_key=True
    )
    league_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("leagues.id"), primary_key=True
    )
    season: Mapped[int] = mapped_column(Integer, primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    form: Mapped[str | None] = mapped_column(Text, nullable=True)
    played_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    played_away: Mapped[int | None] = mapped_column(Integer, nullable=True)
    played_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wins_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wins_away: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wins_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draws_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draws_away: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draws_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    loses_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    loses_away: Mapped[int | None] = mapped_column(Integer, nullable=True)
    loses_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gf_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gf_away: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gf_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ga_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ga_away: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ga_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gf_avg_home: Mapped[str | None] = mapped_column(String(16), nullable=True)
    gf_avg_away: Mapped[str | None] = mapped_column(String(16), nullable=True)
    gf_avg_total: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ga_avg_home: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ga_avg_away: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ga_avg_total: Mapped[str | None] = mapped_column(String(16), nullable=True)
    gf_minute: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ga_minute: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    streak_wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    streak_draws: Mapped[int | None] = mapped_column(Integer, nullable=True)
    streak_loses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    biggest_win_home: Mapped[str | None] = mapped_column(String(16), nullable=True)
    biggest_win_away: Mapped[str | None] = mapped_column(String(16), nullable=True)
    biggest_loss_home: Mapped[str | None] = mapped_column(String(16), nullable=True)
    biggest_loss_away: Mapped[str | None] = mapped_column(String(16), nullable=True)
    biggest_gf_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    biggest_gf_away: Mapped[int | None] = mapped_column(Integer, nullable=True)
    biggest_ga_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    biggest_ga_away: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cs_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cs_away: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cs_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fts_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fts_away: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fts_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pen_scored: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pen_scored_pct: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pen_missed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pen_missed_pct: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pen_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lineups: Mapped[list[Any] | dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    cards_yellow: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cards_red: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
