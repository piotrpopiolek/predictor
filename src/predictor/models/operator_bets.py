"""ORM for operator next-goal bet ledger (status process only)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from predictor.models.base import Base


class OperatorBet(Base):
    __tablename__ = "operator_bets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'won', 'lost', 'void')",
            name="operator_bets_status_check",
        ),
        CheckConstraint(
            "odd >= 1.200 AND odd <= 6.000",
            name="operator_bets_odd_check",
        ),
        CheckConstraint("stake > 0", name="operator_bets_stake_check"),
        Index(
            "uq_operator_bets_open_fixture",
            "fixture_id",
            unique=True,
            postgresql_where="status = 'open'",
        ),
        Index("idx_operator_bets_placed", "placed_at"),
        Index("idx_operator_bets_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id", ondelete="RESTRICT"), nullable=False
    )
    odd: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    stake: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="open")
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    league: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    home_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    away_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    goals_home: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    goals_away: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    elapsed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status_short: Mapped[str | None] = mapped_column(String(10), nullable=True)
    payout_keep: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=Decimal("0.880")
    )
