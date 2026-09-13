"""ORM for /injuries."""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from predictor.models.base import Base


class Injury(Base):
    __tablename__ = "injuries"
    __table_args__ = (UniqueConstraint("fixture_id", "player_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("players.id"), nullable=False
    )
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id"), nullable=False
    )
    fixture_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("fixtures.id"), nullable=True
    )
    league_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("leagues.id"), nullable=True
    )
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    availability: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
