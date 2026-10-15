"""Enrichment claims: fixture detail, injuries, half stats."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.constants import (
    ENRICHABLE_FIXTURE_STATUSES,
)
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.services.queue.core import (
    _due_clause,
    _mark_claimed,
    _require_transaction,
)


async def ensure_enrichment_task(session: AsyncSession, fixture_id: int) -> None:
    _require_transaction(session, "ensure enrichment")
    existing = await session.scalar(
        select(EtlTask.id)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.fixture_id == fixture_id)
        .where(EtlTask.cursor_kind.is_(None))
    )
    if existing is None:
        session.add(
            EtlTask(
                endpoint="/fixtures",
                fixture_id=fixture_id,
                params={"id": fixture_id},
                status="pending",
            )
        )


async def ensure_injuries_task(
    session: AsyncSession, league_id: int, season: int
) -> None:
    _require_transaction(session, "ensure injuries")
    params = {"league": league_id, "season": season}
    existing = await session.scalar(
        select(EtlTask.id)
        .where(EtlTask.endpoint == "/injuries")
        .where(EtlTask.params.contains(params))
        .where(EtlTask.cursor_kind.is_(None))
    )
    if existing is None:
        session.add(
            EtlTask(
                endpoint="/injuries",
                params=params,
                status="pending",
            )
        )


async def ensure_half_stats_task(session: AsyncSession, fixture_id: int) -> None:
    _require_transaction(session, "ensure half stats")
    params = {"fixture": fixture_id, "half": "true"}
    existing = await session.scalar(
        select(EtlTask.id)
        .where(EtlTask.endpoint == "/fixtures/statistics")
        .where(EtlTask.fixture_id == fixture_id)
        .where(EtlTask.cursor_kind.is_(None))
    )
    if existing is None:
        session.add(
            EtlTask(
                endpoint="/fixtures/statistics",
                fixture_id=fixture_id,
                params=params,
                status="pending",
            )
        )


async def claim_enrichment_task(session: AsyncSession) -> EtlTask | None:
    _require_transaction(session, "claim enrichment")
    stmt = (
        select(EtlTask)
        .join(Fixture, Fixture.id == EtlTask.fixture_id)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.fixture_id.is_not(None))
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.status.in_(("pending", "retryable_error")))
        .where(_due_clause(datetime.now(UTC)))
        .where(Fixture.status_short.in_(tuple(ENRICHABLE_FIXTURE_STATUSES)))
        .order_by(EtlTask.id)
        .with_for_update(skip_locked=True, of=EtlTask)
        .limit(1)
    )
    task = await session.scalar(stmt)
    if task is None:
        return None
    return _mark_claimed(task)


async def claim_half_stats_task(session: AsyncSession) -> EtlTask | None:
    _require_transaction(session, "claim half stats")
    stmt = (
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures/statistics")
        .where(EtlTask.status.in_(("pending", "retryable_error")))
        .where(_due_clause(datetime.now(UTC)))
        .where(EtlTask.cursor_kind.is_(None))
        .order_by(EtlTask.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    task = await session.scalar(stmt)
    if task is None:
        return None
    return _mark_claimed(task)


async def claim_injuries_task(session: AsyncSession) -> EtlTask | None:
    _require_transaction(session, "claim injuries")
    stmt = (
        select(EtlTask)
        .where(EtlTask.endpoint == "/injuries")
        .where(EtlTask.status.in_(("pending", "retryable_error")))
        .where(_due_clause(datetime.now(UTC)))
        .where(EtlTask.cursor_kind.is_(None))
        .order_by(EtlTask.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    task = await session.scalar(stmt)
    if task is None:
        return None
    return _mark_claimed(task)


async def claim_control_refresh_task(
    session: AsyncSession, now: datetime
) -> EtlTask | None:
    _require_transaction(session, "claim control refresh")
    due = now.astimezone(UTC).isoformat()
    stmt = (
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.fixture_id.is_not(None))
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.status == "complete")
        .where(EtlTask.params.contains({"control_done": False}))
        .where(EtlTask.params["control_due"].as_string() <= due)
        .order_by(EtlTask.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    task = await session.scalar(stmt)
    if task is None:
        return None
    return _mark_claimed(task)
