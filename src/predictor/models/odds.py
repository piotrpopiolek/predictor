"""ORM for live odds snapshots. History is append-only (captured_at in PK)."""

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
