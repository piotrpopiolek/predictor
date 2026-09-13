"""ORM for W3 dictionary tables (countries, seasons, leagues, odds dictionaries)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from predictor.models.base import Base


class Country(Base):
    __tablename__ = "countries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    flag: Mapped[str | None] = mapped_column(Text, nullable=True)


class Season(Base):
    __tablename__ = "seasons"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)


class League(Base):
    __tablename__ = "leagues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    logo: Mapped[str | None] = mapped_column(Text, nullable=True)
    country_name: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("countries.name"), nullable=True
    )
    country_code: Mapped[str | None] = mapped_column(String(8), nullable=True)


class LeagueSeason(Base):
    __tablename__ = "league_seasons"

    league_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("leagues.id"), primary_key=True
    )
    season_year: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_current: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cov_events: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cov_lineups: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cov_statistics_fixtures: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cov_statistics_players: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cov_standings: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cov_players: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cov_top_scorers: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cov_top_assists: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cov_top_cards: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cov_injuries: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cov_predictions: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cov_odds: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class Bookmaker(Base):
    __tablename__ = "bookmakers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class OddsBet(Base):
    __tablename__ = "odds_bets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class OddsLiveBet(Base):
    __tablename__ = "odds_live_bets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    firstname: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lastname: Mapped[str | None] = mapped_column(String(100), nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    birth_place: Mapped[str | None] = mapped_column(String(255), nullable=True)
    birth_country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(100), nullable=True)
    height: Mapped[str | None] = mapped_column(String(20), nullable=True)
    weight: Mapped[str | None] = mapped_column(String(20), nullable=True)
    injured: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    photo: Mapped[str | None] = mapped_column(Text, nullable=True)


class Coach(Base):
    __tablename__ = "coaches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    firstname: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lastname: Mapped[str | None] = mapped_column(String(100), nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    birth_place: Mapped[str | None] = mapped_column(String(255), nullable=True)
    birth_country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(100), nullable=True)
    height: Mapped[str | None] = mapped_column(String(20), nullable=True)
    weight: Mapped[str | None] = mapped_column(String(20), nullable=True)
    photo: Mapped[str | None] = mapped_column(Text, nullable=True)
    team_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("teams.id"), nullable=True
    )
