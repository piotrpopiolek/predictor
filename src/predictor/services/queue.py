"""etl_tasks queue, cursors, and checkpoints (FR-025, FR-026, FR-027, FR-029)."""

from __future__ import annotations

import os
import socket
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.constants import CURSOR_KINDS, DICTIONARY_ENDPOINTS, ETL_STATUSES
from predictor.models.etl import EtlRun, EtlTask
from predictor.schemas.settings import Settings
from predictor.services.lock import WriterLock

TERMINAL_STATUSES = frozenset(
    {"complete", "coverage_empty", "not_supported", "permanent_error"}
)


def _require_transaction(session: AsyncSession, action: str) -> None:
    if not session.in_transaction():
        raise RuntimeError(f"{action} must run inside a transaction")


async def record_run_start(
    session: AsyncSession, settings: Settings, lock: WriterLock
) -> int:
    _require_transaction(session, "run start")
    run = EtlRun(
        host=socket.gethostname(),
        host_environment=settings.host_environment.value,
        pid=os.getpid(),
        instance_id=settings.instance_id,
        lock_held=True,
        postgres_backend_pid=lock.backend_pid,
    )
    session.add(run)
    await session.flush()
    return int(run.id)


async def record_run_end(session: AsyncSession, run_id: int) -> None:
    _require_transaction(session, "run end")
    run = await session.get(EtlRun, run_id)
    if run is None:
        return
    run.ended_at = datetime.now(UTC)
    run.lock_held = False


async def ensure_cursors(session: AsyncSession) -> None:
    _require_transaction(session, "ensure cursors")
    for kind in CURSOR_KINDS:
        existing = await session.scalar(
            select(EtlTask.id).where(EtlTask.cursor_kind == kind)
        )
        if existing is None:
            session.add(
                EtlTask(
                    endpoint=f"cursor/{kind}",
                    params={},
                    cursor_kind=kind,
                    status="pending",
                )
            )


async def requeue_orphans(session: AsyncSession) -> int:
    _require_transaction(session, "requeue orphans")
    result = await session.execute(
        update(EtlTask)
        .where(EtlTask.status == "in_progress")
        .values(
            status="pending",
            last_error="orphaned_in_progress",
            attempt_count=EtlTask.attempt_count + 1,
            updated_at=func.now(),
        )
        .returning(EtlTask.id)
    )
    return len(list(result.scalars().all()))


async def ensure_dictionary_tasks(session: AsyncSession) -> None:
    _require_transaction(session, "ensure dictionary tasks")
    for endpoint in DICTIONARY_ENDPOINTS:
        existing = await session.scalar(
            select(EtlTask.id)
            .where(EtlTask.endpoint == endpoint)
            .where(EtlTask.cursor_kind.is_(None))
        )
        if existing is None:
            session.add(EtlTask(endpoint=endpoint, params={}, status="pending"))


async def get_or_create_endpoint_task(session: AsyncSession, endpoint: str) -> EtlTask:
    _require_transaction(session, "get or create task")
    task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == endpoint)
        .where(EtlTask.cursor_kind.is_(None))
        .order_by(EtlTask.id)
        .limit(1)
    )
    if task is None:
        task = EtlTask(endpoint=endpoint, params={}, status="pending")
        session.add(task)
        await session.flush()
    return task


def needs_refresh(task: EtlTask, now: datetime) -> bool:
    if task.status in {"pending", "retryable_error"}:
        return True
    if task.status == "complete" and task.completed_at is not None:
        completed = task.completed_at
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=UTC)
        return completed.astimezone(UTC).date() < now.astimezone(UTC).date()
    return task.status not in {
        "complete",
        "coverage_empty",
        "not_supported",
        "permanent_error",
    }


async def claim_next(session: AsyncSession) -> EtlTask | None:
    _require_transaction(session, "claim")
    stmt = (
        select(EtlTask)
        .where(EtlTask.status.in_(("pending", "retryable_error")))
        .where(EtlTask.cursor_kind.is_(None))
        .order_by(EtlTask.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    task = await session.scalar(stmt)
    if task is None:
        return None
    now = datetime.now(UTC)
    task.status = "in_progress"
    task.started_at = now
    task.updated_at = now
    task.attempt_count = task.attempt_count + 1
    return task


async def complete_task(
    session: AsyncSession,
    task: EtlTask,
    status: str,
    *,
    error: str | None = None,
    paging_current: int | None = None,
    paging_total: int | None = None,
    params: dict[str, Any] | None = None,
) -> None:
    """Mark completeness in the caller's open transaction (FR-026). Does not commit."""
    _require_transaction(session, "checkpoint")
    if status not in ETL_STATUSES:
        raise ValueError(f"invalid etl status: {status}")
    now = datetime.now(UTC)
    task.status = status
    task.updated_at = now
    if error is not None:
        task.last_error = error
    if paging_current is not None:
        task.paging_current = paging_current
    if paging_total is not None:
        task.paging_total = paging_total
    if params is not None:
        task.params = params
    if status in TERMINAL_STATUSES:
        task.completed_at = now


async def get_or_create_day_task(
    session: AsyncSession, endpoint: str, day: date
) -> EtlTask:
    _require_transaction(session, "get or create day task")
    task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == endpoint)
        .where(EtlTask.day_utc == day)
        .where(EtlTask.fixture_id.is_(None))
        .where(EtlTask.cursor_kind.is_(None))
        .order_by(EtlTask.id)
        .limit(1)
    )
    if task is None:
        task = EtlTask(
            endpoint=endpoint,
            day_utc=day,
            params={"date": day.isoformat()},
            status="pending",
        )
        session.add(task)
        await session.flush()
    return task


async def get_or_create_live_fixtures_task(session: AsyncSession) -> EtlTask:
    _require_transaction(session, "get or create live fixtures")
    params = {"live": "all"}
    task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.fixture_id.is_(None))
        .where(EtlTask.day_utc.is_(None))
        .where(EtlTask.params.contains(params))
        .order_by(EtlTask.id)
        .limit(1)
    )
    if task is None:
        task = EtlTask(
            endpoint="/fixtures",
            params=params,
            status="pending",
        )
        session.add(task)
        await session.flush()
    return task


async def get_cursor_task(session: AsyncSession, kind: str) -> EtlTask:
    _require_transaction(session, "get cursor")
    task = await session.scalar(select(EtlTask).where(EtlTask.cursor_kind == kind))
    if task is None:
        await ensure_cursors(session)
        await session.flush()
        task = await session.scalar(select(EtlTask).where(EtlTask.cursor_kind == kind))
    if task is None:
        raise RuntimeError(f"missing cursor {kind}")
    return task


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


async def ensure_rounds_task(
    session: AsyncSession, league_id: int, season: int
) -> None:
    _require_transaction(session, "ensure rounds")
    params = {"league": league_id, "season": season}
    existing = await session.scalar(
        select(EtlTask.id)
        .where(EtlTask.endpoint == "/fixtures/rounds")
        .where(EtlTask.params.contains(params))
        .where(EtlTask.cursor_kind.is_(None))
    )
    if existing is None:
        session.add(
            EtlTask(
                endpoint="/fixtures/rounds",
                params=params,
                status="pending",
            )
        )


async def claim_rounds_task(session: AsyncSession) -> EtlTask | None:
    _require_transaction(session, "claim rounds")
    stmt = (
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures/rounds")
        .where(EtlTask.status.in_(("pending", "retryable_error")))
        .where(EtlTask.cursor_kind.is_(None))
        .order_by(EtlTask.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    task = await session.scalar(stmt)
    if task is None:
        return None
    now = datetime.now(UTC)
    task.status = "in_progress"
    task.started_at = now
    task.updated_at = now
    task.attempt_count = task.attempt_count + 1
    return task
