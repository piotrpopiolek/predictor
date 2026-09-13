"""ORM for transfers, sidelined, coach career, trophies."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from predictor.models.base import Base


class Transfer(Base):
    __tablename__ = "transfers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("players.id"), nullable=False
    )
    from_team_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("teams.id"), nullable=True
    )
    to_team_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("teams.id"), nullable=True
    )
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    api_update: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Sidelined(Base):
    __tablename__ = "sidelined"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    player_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("players.id"), nullable=True
    )
    coach_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("coaches.id"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class CoachCareer(Base):
    __tablename__ = "coach_career"

    coach_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("coaches.id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id"), primary_key=True
    )
    start_date: Mapped[date] = mapped_column(Date, primary_key=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class Trophy(Base):
    __tablename__ = "trophies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    player_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("players.id"), nullable=True
    )
    coach_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("coaches.id"), nullable=True
    )
    league_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    season: Mapped[str | None] = mapped_column(String(32), nullable=True)
    place: Mapped[str | None] = mapped_column(String(100), nullable=True)
