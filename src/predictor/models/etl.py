"""ORM for etl_runs and etl_tasks (W2). Domain models arrive with ingest waves."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from predictor.models.base import Base


class EtlRun(Base):
    __tablename__ = "etl_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    host_environment: Mapped[str] = mapped_column(String(8), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    instance_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lock_held: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    postgres_backend_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)


class EtlTask(Base):
    __tablename__ = "etl_tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("etl_runs.id", ondelete="SET NULL"), nullable=True
    )
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    cursor_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fixture_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    day_utc: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    paging_current: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paging_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
