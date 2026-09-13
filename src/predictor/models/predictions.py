"""ORM for /predictions and prediction_h2h (H2H fixtures must exist first)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from predictor.models.base import Base


class Prediction(Base):
    __tablename__ = "predictions"

    fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id", ondelete="CASCADE"), primary_key=True
    )
    winner_team_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("teams.id"), nullable=True
    )
    winner_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    win_or_draw: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    under_over: Mapped[str | None] = mapped_column(String(16), nullable=True)
    goals_home: Mapped[str | None] = mapped_column(String(16), nullable=True)
    goals_away: Mapped[str | None] = mapped_column(String(16), nullable=True)
    advice: Mapped[str | None] = mapped_column(Text, nullable=True)
    pct_home: Mapped[str | None] = mapped_column(String(8), nullable=True)
    pct_draw: Mapped[str | None] = mapped_column(String(8), nullable=True)
    pct_away: Mapped[str | None] = mapped_column(String(8), nullable=True)
    comparison: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    teams: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PredictionH2H(Base):
    __tablename__ = "prediction_h2h"

    fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id", ondelete="CASCADE"), primary_key=True
    )
    h2h_fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id"), primary_key=True
    )
    sort_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
