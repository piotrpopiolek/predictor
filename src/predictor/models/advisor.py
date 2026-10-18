"""ORM for the phase-2 decision ledger. Written only by predictor-advisor."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from predictor.models.base import Base

_PERIOD = "period IN ('regulation', 'extra_time')"
_MODE = "mode IN ('sandbox', 'recommendation')"


class AdvisorStrategy(Base):
    __tablename__ = "advisor_strategies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'sandbox', 'promoted', 'paused', 'retired')",
            name="advisor_strategies_status_check",
        ),
        CheckConstraint(
            "kind IN ('llm', 'baseline')",
            name="advisor_strategies_kind_check",
        ),
        Index(
            "uq_advisor_strategies_one_promoted",
            "status",
            unique=True,
            postgresql_where="status = 'promoted'",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdvisorStrategyVersion(Base):
    """Immutable. A new idea is a new row, never an update of this one."""

    __tablename__ = "advisor_strategy_versions"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id", "version_no", name="uq_advisor_strategy_versions"
        ),
        CheckConstraint(
            "version_no >= 1", name="advisor_strategy_versions_version_check"
        ),
        CheckConstraint(
            "min_combined_odd > 1", name="advisor_strategy_versions_odd_check"
        ),
        CheckConstraint(
            "max_legs BETWEEN 1 AND 3", name="advisor_strategy_versions_legs_check"
        ),
        CheckConstraint(
            "max_minute_regulation BETWEEN 1 AND 120",
            name="advisor_strategy_versions_minute_check",
        ),
        CheckConstraint(
            "max_minute_extra BETWEEN 91 AND 130",
            name="advisor_strategy_versions_extra_minute_check",
        ),
        CheckConstraint(
            "wait_minutes BETWEEN 1 AND 30",
            name="advisor_strategy_versions_wait_check",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("advisor_strategies.id", ondelete="RESTRICT")
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    min_combined_odd: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    max_legs: Mapped[int] = mapped_column(Integer, nullable=False)
    max_minute_regulation: Mapped[int] = mapped_column(Integer, nullable=False)
    max_minute_extra: Mapped[int] = mapped_column(Integer, nullable=False)
    wait_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_rules: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdvisorCycle(Base):
    """One model call. Insert-only; the application never updates the row."""

    __tablename__ = "advisor_cycles"
    __table_args__ = (
        CheckConstraint(
            "validation_status IN ('ok', 'invalid_output', 'provider_error')",
            name="advisor_cycles_validation_check",
        ),
        CheckConstraint(_MODE, name="advisor_cycles_mode_check"),
        CheckConstraint("cost_pln >= 0", name="advisor_cycles_cost_check"),
        CheckConstraint("thoughts_tokens >= 0", name="advisor_cycles_thoughts_check"),
        Index("idx_advisor_cycles_started", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strategy_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("advisor_strategy_versions.id", ondelete="RESTRICT"),
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thoughts_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_pln: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    otel_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AdvisorDecision(Base):
    """One candidate inside a cycle, or a wait timeout with no cycle. Insert-only."""

    __tablename__ = "advisor_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('bet', 'reject', 'wait', 'wait_timeout')",
            name="advisor_decisions_decision_check",
        ),
        CheckConstraint(_PERIOD, name="advisor_decisions_period_check"),
        CheckConstraint(
            "estimated_probability IS NULL OR "
            "(estimated_probability >= 0 AND estimated_probability <= 1)",
            name="advisor_decisions_probability_check",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="advisor_decisions_confidence_check",
        ),
        Index(
            "uq_advisor_decisions_rejected_state",
            "strategy_id",
            "fixture_id",
            "period",
            "goals_home",
            "goals_away",
            "ordinal",
            unique=True,
            postgresql_where="decision IN ('reject', 'wait_timeout')",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("advisor_cycles.id", ondelete="RESTRICT"), nullable=True
    )
    strategy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("advisor_strategies.id", ondelete="RESTRICT")
    )
    fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id", ondelete="RESTRICT")
    )
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    goals_home: Mapped[int] = mapped_column(Integer, nullable=False)
    goals_away: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_rule: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    estimated_probability: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 5), nullable=True
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    odd_at_eval: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    n: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdvisorCounterfactual(Base):
    """Whether a goal came after the decision.

    A separate row, so the decision itself is never updated.
    """

    __tablename__ = "advisor_counterfactuals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("advisor_decisions.id", ondelete="RESTRICT"),
        unique=True,
    )
    goal_scored: Mapped[bool] = mapped_column(Boolean, nullable=False)
    odd: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdvisorCoupon(Base):
    __tablename__ = "advisor_coupons"
    __table_args__ = (
        CheckConstraint(_MODE, name="advisor_coupons_mode_check"),
        CheckConstraint(
            "status IN ('open', 'proposed', 'accepted', 'modified', 'rejected', "
            "'expired', 'placed', 'not_placed', 'won', 'lost', 'void')",
            name="advisor_coupons_status_check",
        ),
        CheckConstraint("stake > 0", name="advisor_coupons_stake_check"),
        CheckConstraint(
            "combined_odd IS NULL OR combined_odd > 1",
            name="advisor_coupons_odd_check",
        ),
        CheckConstraint(
            "payout_keep > 0 AND payout_keep <= 1",
            name="advisor_coupons_keep_check",
        ),
        Index("idx_advisor_coupons_version", "strategy_version_id"),
        Index("idx_advisor_coupons_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("advisor_strategy_versions.id", ondelete="RESTRICT"),
    )
    cycle_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("advisor_cycles.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    stake: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    combined_odd: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    proposed_combined_odd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 3), nullable=True
    )
    payout_keep: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=Decimal("0.880")
    )
    placed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdvisorLeg(Base):
    """One match on a coupon.

    ``strategy_id`` and ``mode`` repeat the coupon so the unique index can
    see them. A partial index cannot join to ``advisor_coupons``.
    """

    __tablename__ = "advisor_legs"
    __table_args__ = (
        UniqueConstraint(
            "coupon_id", "fixture_id", name="uq_advisor_legs_coupon_fixture"
        ),
        CheckConstraint(_PERIOD, name="advisor_legs_period_check"),
        CheckConstraint(_MODE, name="advisor_legs_mode_check"),
        CheckConstraint(
            "status IN ('open', 'won', 'lost', 'void', 'removed', 'manual')",
            name="advisor_legs_status_check",
        ),
        CheckConstraint("n >= 0", name="advisor_legs_n_check"),
        CheckConstraint("odd > 1", name="advisor_legs_odd_check"),
        Index(
            "uq_advisor_legs_open_sandbox",
            "strategy_id",
            "fixture_id",
            unique=True,
            postgresql_where="status IN ('open', 'manual') AND mode = 'sandbox'",
        ),
        Index(
            "uq_advisor_legs_open_recommendation",
            "fixture_id",
            unique=True,
            postgresql_where=(
                "status IN ('open', 'manual') AND mode = 'recommendation'"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    coupon_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("advisor_coupons.id", ondelete="RESTRICT")
    )
    strategy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("advisor_strategies.id", ondelete="RESTRICT")
    )
    fixture_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fixtures.id", ondelete="RESTRICT")
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    odd: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    proposed_odd: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    goals_home: Mapped[int] = mapped_column(Integer, nullable=False)
    goals_away: Mapped[int] = mapped_column(Integer, nullable=False)
    elapsed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdvisorSettlementEvent(Base):
    __tablename__ = "advisor_settlement_events"
    __table_args__ = (
        CheckConstraint(
            "source IN ('settle', 'correction', 'manual')",
            name="advisor_settlement_events_source_check",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    coupon_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("advisor_coupons.id", ondelete="RESTRICT")
    )
    leg_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("advisor_legs.id", ondelete="RESTRICT"), nullable=True
    )
    previous_status: Mapped[str] = mapped_column(String(16), nullable=False)
    new_status: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdvisorLangfuseOutbox(Base):
    """One export attempt list for a cycle. Status changes; the cycle does not."""

    __tablename__ = "advisor_langfuse_outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'abandoned')",
            name="advisor_langfuse_outbox_status_check",
        ),
        CheckConstraint(
            "attempts >= 0 AND attempts <= 5",
            name="advisor_langfuse_outbox_attempts_check",
        ),
        Index(
            "idx_advisor_langfuse_outbox_pending",
            "id",
            postgresql_where="status = 'pending'",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("advisor_cycles.id", ondelete="RESTRICT"),
        unique=True,
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
