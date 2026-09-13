"""ORM for W4: teams, venues, fixtures, league rounds, fixture statuses."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from predictor.models.base import Base


class FixtureStatusRow(Base):
    __tablename__ = "fixture_statuses"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    label: Mapped[str] = mapped_column(String(100), nullable=False)


class Venue(Base):
    __tablename__ = "venues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country_name: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("countries.name"), nullable=True
    )
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    surface: Mapped[str | None] = mapped_column(String(50), nullable=True)
    image: Mapped[str | None] = mapped_column(Text, nullable=True)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    country_name: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("countries.name"), nullable=True
    )
    founded: Mapped[int | None] = mapped_column(Integer, nullable=True)
    national: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    logo: Mapped[str | None] = mapped_column(Text, nullable=True)
    venue_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("venues.id"), nullable=True
    )


class LeagueRound(Base):
    __tablename__ = "league_rounds"

    league_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("leagues.id"), primary_key=True
    )
    season: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    dates: Mapped[list[date] | None] = mapped_column(ARRAY(Date), nullable=True)


class Fixture(Base):
    __tablename__ = "fixtures"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    referee: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    timestamp_utc: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    period_first: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    period_second: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    venue_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("venues.id"), nullable=True
    )
    venue_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    venue_city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status_short: Mapped[str | None] = mapped_column(
        String(10), ForeignKey("fixture_statuses.code"), nullable=True
    )
    status_long: Mapped[str | None] = mapped_column(String(100), nullable=True)
    elapsed_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    league_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("leagues.id"), nullable=False
    )
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    round: Mapped[str | None] = mapped_column(String(100), nullable=True)
    home_team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id"), nullable=False
    )
    away_team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id"), nullable=False
    )
    home_winner: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    away_winner: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    goals_home: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_away: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_home_halftime: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_away_halftime: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_home_fulltime: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_away_fulltime: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_home_extratime: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_away_extratime: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_home_penalty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    goals_away_penalty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tie_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    leg: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, server_default=func.now()
    )
