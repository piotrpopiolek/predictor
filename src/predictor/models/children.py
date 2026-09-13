"""ORM for fixture children: events, lineups, statistics, player stats."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from predictor.models.base import Base


class FixtureEvent(Base):
    __tablename__ = "fixture_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id", ondelete="CASCADE"), nullable=False
    )
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey("teams.id"), nullable=False)
    player_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("players.id"), nullable=True
    )
    assist_player_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("players.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(100), nullable=True)
    minute: Mapped[int] = mapped_column(Integer, nullable=False)
    minute_extra: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FixtureLineup(Base):
    __tablename__ = "fixture_lineups"

    fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id"), primary_key=True
    )
    coach_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("coaches.id"), nullable=True
    )
    formation: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_home: Mapped[bool] = mapped_column(Boolean, nullable=False)
    colors: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)


class FixtureLineupPlayer(Base):
    __tablename__ = "fixture_lineup_players"
    __table_args__ = (
        ForeignKeyConstraint(
            ["fixture_id", "team_id"],
            ["fixture_lineups.fixture_id", "fixture_lineups.team_id"],
            ondelete="CASCADE",
        ),
    )

    fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("players.id"), primary_key=True
    )
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[str | None] = mapped_column(String(8), nullable=True)
    grid: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_starter: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class FixtureStatistic(Base):
    __tablename__ = "fixture_statistics"

    fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id"), primary_key=True
    )
    stat_type: Mapped[str] = mapped_column(String(100), primary_key=True)
    stat_value: Mapped[str | None] = mapped_column(String(50), nullable=True)
    period: Mapped[str] = mapped_column(String(4), primary_key=True, default="FT")


class FixturePlayerStats(Base):
    __tablename__ = "fixture_player_stats"

    fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id"), primary_key=True
    )
    player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("players.id"), primary_key=True
    )
    stats_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rating: Mapped[str | None] = mapped_column(String(8), nullable=True)
    captain: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    substitute: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    offsides: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    yellow_cards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    red_cards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pen_won: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pen_committed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pen_scored: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pen_missed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pen_saved: Mapped[int | None] = mapped_column(Integer, nullable=True)
