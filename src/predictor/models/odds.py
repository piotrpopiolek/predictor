"""ORM for pre-match odds, mapping snapshot, and append-only live snapshots."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from predictor.models.base import Base


class FixtureOdds(Base):
    __tablename__ = "fixture_odds"

    fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id", ondelete="CASCADE"), primary_key=True
    )
    bookmaker_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bookmakers.id"), primary_key=True
    )
    bet_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("odds_bets.id"), primary_key=True
    )
    value_label: Mapped[str] = mapped_column(String(64), primary_key=True)
    odd: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    total: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    handicap: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    api_update: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OddsFixtureMapping(Base):
    __tablename__ = "odds_fixture_mapping"

    fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id", ondelete="CASCADE"), primary_key=True
    )
    league_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("leagues.id"), nullable=True
    )
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class FixtureOddsLive(Base):
    __tablename__ = "fixture_odds_live"

    fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id", ondelete="CASCADE"), primary_key=True
    )
    bet_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("odds_live_bets.id"), primary_key=True
    )
    value_label: Mapped[str] = mapped_column(String(64), primary_key=True)
    handicap: Mapped[str] = mapped_column(String(16), primary_key=True, default="")
    odd: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    is_main: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    suspended: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    stopped: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    blocked: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    finished: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status_long: Mapped[str | None] = mapped_column(String(100), nullable=True)
    elapsed_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elapsed_seconds: Mapped[str | None] = mapped_column(String(16), nullable=True)
    league_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_goals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_goals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_update: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
        server_default=func.now(),
    )
