"""Shared etl_tasks primitives: cursors, checkpoints, claim helpers."""

from __future__ import annotations

import os
import socket
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Date, and_, cast, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.constants import (
    CURSOR_KINDS,
    DICTIONARY_ENDPOINTS,
    ERROR_RETRY_BASE_BACKOFF_SECONDS,
    ERROR_RETRY_LIMIT,
    ERROR_RETRY_MAX_BACKOFF_SECONDS,
    ETL_STATUSES,
    FINISHED_FIXTURE_STATUSES,
    HISTORICAL_BACKLOG_MAX_AGE_SECONDS,
    IN_PLAY_FIXTURE_STATUSES,
    LIVE_CONTEXT_EMPTY_BACKOFF_SECONDS,
    LIVE_CONTEXT_MAX_EMPTY_ATTEMPTS,
    MATCH_LIVE_HOURS,
    ODDS_MAPPING_REFRESH_SECONDS,
    ODDS_MAPPING_RETRY_BACKOFF_SECONDS,
    OPEN_ETL_STATUSES,
    PARKED_PREMATCH_STATUSES,
    URGENT_PREMATCH_HORIZON_HOURS,
)
from predictor.models.catalog import UntrackedLeague
from predictor.models.etl import EtlRun, EtlTask
from predictor.models.fixtures import Fixture
from predictor.schemas.settings import Settings
from predictor.services.lock import WriterLock
from predictor.telemetry import start_span

TERMINAL_STATUSES = frozenset(
    {"complete", "coverage_empty", "not_supported", "permanent_error"}
)


def empty_retry_status(
    params: dict[str, Any],
    *,
    now: datetime,
    max_attempts: int = LIVE_CONTEXT_MAX_EMPTY_ATTEMPTS,
    backoff_seconds: int = LIVE_CONTEXT_EMPTY_BACKOFF_SECONDS,
) -> tuple[str, dict[str, Any]]:
    """Retry an empty/coverage-false fetch, then stop as coverage_empty."""
    merged = dict(params)
    attempts = int(merged.get("empty_attempts") or 0) + 1
    merged["empty_attempts"] = attempts
    if attempts >= max_attempts:
        merged.pop("next_attempt_at", None)
        return "coverage_empty", merged
    due = now.astimezone(UTC) + timedelta(seconds=backoff_seconds)
    merged["next_attempt_at"] = due.isoformat()
    return "retryable_error", merged


def error_retry_status(
    params: dict[str, Any],
    *,
    now: datetime,
    max_retries: int = ERROR_RETRY_LIMIT,
    base_backoff_seconds: int = ERROR_RETRY_BASE_BACKOFF_SECONDS,
    max_backoff_seconds: int = ERROR_RETRY_MAX_BACKOFF_SECONDS,
) -> tuple[str, dict[str, Any]]:
    """Put a failed task back up to ``max_retries`` times, then stop.

    The wait doubles on each retry, starting at ``base_backoff_seconds``,
    and does not grow past ``max_backoff_seconds``.
    """
    merged = dict(params)
    retries = int(merged.get("error_retries") or 0) + 1
    merged["error_retries"] = retries
    if retries > max_retries:
        merged.pop("next_attempt_at", None)
        return "permanent_error", merged
    delay = min(max_backoff_seconds, base_backoff_seconds * (2 ** (retries - 1)))
    due = now.astimezone(UTC) + timedelta(seconds=delay)
    merged["next_attempt_at"] = due.isoformat()
    return "retryable_error", merged


def _due_clause(now: datetime) -> Any:
    stamp = now.astimezone(UTC).isoformat()
    return or_(
        EtlTask.status == "pending",
        ~EtlTask.params.has_key("next_attempt_at"),
        EtlTask.params["next_attempt_at"].as_string() <= stamp,
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
        .where(EtlTask.fixture_id.is_(None))
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


def mapping_needs_refresh(task: EtlTask, now: datetime) -> bool:
    if task.status == "retryable_error":
        stamp = task.updated_at
        if stamp is None:
            return True
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        wait = timedelta(seconds=ODDS_MAPPING_RETRY_BACKOFF_SECONDS)
        return stamp.astimezone(UTC) + wait <= now.astimezone(UTC)
    if task.status == "complete" and task.completed_at is not None:
        completed = task.completed_at
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=UTC)
        wait = timedelta(seconds=ODDS_MAPPING_REFRESH_SECONDS)
        return completed.astimezone(UTC) + wait <= now.astimezone(UTC)
    return needs_refresh(task, now)


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
    with start_span(
        "etl.transaction",
        etl_endpoint=task.endpoint,
        etl_result=status,
    ):
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


def _mark_claimed(task: EtlTask) -> EtlTask:
    now = datetime.now(UTC)
    task.status = "in_progress"
    task.started_at = now
    task.updated_at = now
    task.attempt_count = task.attempt_count + 1
    return task


def _as_utc(stamp: datetime) -> datetime:
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _parked_prematch_enrichment(stamp: datetime):
    """Pending /fixtures?id= for a kickoff that has not started yet."""
    return and_(
        EtlTask.endpoint == "/fixtures",
        EtlTask.fixture_id.is_not(None),
        EtlTask.cursor_kind.is_(None),
        exists(
            select(1).where(
                Fixture.id == EtlTask.fixture_id,
                Fixture.status_short.in_(tuple(PARKED_PREMATCH_STATUSES)),
                Fixture.date.is_not(None),
                Fixture.date >= stamp,
            )
        ),
    )


def _stale_status_fixture(stamp: datetime):
    """Kickoff already passed but the row still looks prematch or abandoned live."""
    live_deadline = stamp - timedelta(hours=MATCH_LIVE_HOURS)
    return or_(
        and_(
            Fixture.status_short.in_(tuple(PARKED_PREMATCH_STATUSES)),
            Fixture.date.is_not(None),
            Fixture.date < stamp,
        ),
        and_(
            Fixture.status_short.in_(tuple(IN_PLAY_FIXTURE_STATUSES)),
            Fixture.date.is_not(None),
            Fixture.date < live_deadline,
        ),
    )


async def oldest_actionable_pending_at(
    session: AsyncSession, now: datetime | None = None
) -> datetime | None:
    """Oldest open non-cursor task, ignoring parked future prematch enrichment."""
    stamp = _as_utc(now or datetime.now(UTC))
    return await session.scalar(
        select(func.min(EtlTask.created_at)).where(
            EtlTask.status.in_(tuple(OPEN_ETL_STATUSES)),
            EtlTask.cursor_kind.is_(None),
            ~_parked_prematch_enrichment(stamp),
        )
    )


async def historical_backlog_blocks(
    session: AsyncSession, now: datetime | None = None
) -> bool:
    """True when an open task has been waiting longer than a week.

    Same clock as the oldest-pending gauge: ``now - min(created_at)`` over
    actionable open tasks. Prematch ``NS``/``TBD`` enrichment for future
    kickoffs is ignored so it does not freeze backfill. Daily snapshot rows
    are re-dated when they are reopened, so a finished table does not keep
    this gate shut.
    """
    stamp = _as_utc(now or datetime.now(UTC))
    oldest = await oldest_actionable_pending_at(session, stamp)
    if oldest is None:
        return False
    age = stamp - _as_utc(oldest)
    return age.total_seconds() > HISTORICAL_BACKLOG_MAX_AGE_SECONDS


async def stale_fixture_days(
    session: AsyncSession,
    now: datetime | None = None,
    *,
    limit: int,
) -> list[date]:
    """UTC days whose pending fixture enrichment is stuck on a stale status."""
    if limit <= 0:
        return []
    stamp = _as_utc(now or datetime.now(UTC))
    day_utc = cast(func.timezone("UTC", Fixture.date), Date)
    rows = (
        await session.execute(
            select(day_utc)
            .select_from(EtlTask)
            .join(Fixture, Fixture.id == EtlTask.fixture_id)
            .where(EtlTask.endpoint == "/fixtures")
            .where(EtlTask.fixture_id.is_not(None))
            .where(EtlTask.cursor_kind.is_(None))
            .where(EtlTask.status.in_(tuple(OPEN_ETL_STATUSES)))
            .where(_stale_status_fixture(stamp))
            .group_by(day_utc)
            .order_by(day_utc)
            .limit(limit)
        )
    ).all()
    return [row[0] for row in rows if row[0] is not None]


async def close_stale_prematch_enrichment(
    session: AsyncSession, day: date, now: datetime | None = None
) -> int:
    """Close enrichment still stale after day repair (past NS/TBD or abandoned live)."""
    _require_transaction(session, "close stale prematch enrichment")
    stamp = _as_utc(now or datetime.now(UTC))
    day_utc = cast(func.timezone("UTC", Fixture.date), Date)
    result = await session.execute(
        update(EtlTask)
        .where(
            EtlTask.id.in_(
                select(EtlTask.id)
                .join(Fixture, Fixture.id == EtlTask.fixture_id)
                .where(EtlTask.endpoint == "/fixtures")
                .where(EtlTask.fixture_id.is_not(None))
                .where(EtlTask.cursor_kind.is_(None))
                .where(EtlTask.status.in_(tuple(OPEN_ETL_STATUSES)))
                .where(_stale_status_fixture(stamp))
                .where(day_utc == day)
            )
        )
        .values(
            status="coverage_empty",
            last_error="stale_prematch_after_day_repair",
            completed_at=stamp,
            updated_at=stamp,
        )
        .returning(EtlTask.id)
    )
    return len(list(result.scalars().all()))


async def untracked_league_ids(session: AsyncSession) -> set[int]:
    rows = await session.scalars(select(UntrackedLeague.league_id))
    return {int(league_id) for league_id in rows}


async def retire_untracked_tasks(session: AsyncSession) -> set[int]:
    """Close open tasks for leagues we do not follow. Returns those league ids."""
    _require_transaction(session, "retire untracked")
    ignored = await untracked_league_ids(session)
    if not ignored:
        return ignored
    now = datetime.now(UTC)
    league_text = [str(league_id) for league_id in ignored]
    open_status = EtlTask.status.in_(tuple(OPEN_ETL_STATUSES))
    no_cursor = EtlTask.cursor_kind.is_(None)
    by_fixture = (
        select(EtlTask.id)
        .join(Fixture, Fixture.id == EtlTask.fixture_id)
        .where(no_cursor, open_status, Fixture.league_id.in_(ignored))
    )
    by_param = select(EtlTask.id).where(
        no_cursor,
        open_status,
        EtlTask.params["league"].as_string().in_(league_text),
    )
    await session.execute(
        update(EtlTask)
        .where(EtlTask.id.in_(by_fixture.union(by_param)))
        .values(
            status="not_supported",
            last_error="untracked_league",
            updated_at=now,
            completed_at=now,
        )
    )
    return ignored


async def drop_untracked_fixture_ids(
    session: AsyncSession, fixture_ids: Sequence[int]
) -> list[int]:
    """Keep ids whose league is still followed. Unknown ids stay."""
    ids = list(dict.fromkeys(int(fid) for fid in fixture_ids))
    if not ids:
        return []
    ignored = await untracked_league_ids(session)
    if not ignored:
        return ids
    rows = (
        await session.execute(
            select(Fixture.id, Fixture.league_id).where(Fixture.id.in_(ids))
        )
    ).all()
    blocked = {int(fid) for fid, league_id in rows if int(league_id) in ignored}
    return [fid for fid in ids if fid not in blocked]


async def _claim_endpoint_rows(
    session: AsyncSession,
    endpoint: str,
    *,
    limit: int,
    now: datetime | None = None,
) -> list[EtlTask]:
    if limit <= 0:
        return []
    stamp = now or datetime.now(UTC)
    stmt = (
        select(EtlTask)
        .where(EtlTask.endpoint == endpoint)
        .where(EtlTask.status.in_(("pending", "retryable_error")))
        .where(_due_clause(stamp))
        .where(EtlTask.cursor_kind.is_(None))
        .order_by(EtlTask.id)
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    rows = list(await session.scalars(stmt))
    return [_mark_claimed(task) for task in rows]


async def _claim_endpoint(
    session: AsyncSession, endpoint: str, *, now: datetime | None = None
) -> EtlTask | None:
    rows = await _claim_endpoint_rows(session, endpoint, limit=1, now=now)
    return rows[0] if rows else None


async def _claim_fixture_endpoint(
    session: AsyncSession,
    endpoint: str,
    *,
    urgent: bool,
    now: datetime | None = None,
) -> EtlTask | None:
    stamp = now or datetime.now(UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    stmt = (
        select(EtlTask)
        .join(Fixture, Fixture.id == EtlTask.fixture_id)
        .where(EtlTask.endpoint == endpoint)
        .where(EtlTask.fixture_id.is_not(None))
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.status.in_(("pending", "retryable_error")))
        .where(_due_clause(stamp))
    )
    if urgent:
        horizon = stamp + timedelta(hours=URGENT_PREMATCH_HORIZON_HOURS)
        stmt = (
            stmt.where(Fixture.date.is_not(None))
            .where(Fixture.date <= horizon)
            .where(
                (Fixture.status_short.is_(None))
                | (~Fixture.status_short.in_(tuple(FINISHED_FIXTURE_STATUSES)))
            )
        )
    stmt = (
        stmt.order_by(Fixture.date.asc().nulls_last(), EtlTask.id.asc())
        .with_for_update(skip_locked=True, of=EtlTask)
        .limit(1)
    )
    task = await session.scalar(stmt)
    if task is None:
        return None
    return _mark_claimed(task)


async def ensure_param_task(
    session: AsyncSession, endpoint: str, params: dict[str, Any]
) -> None:
    _require_transaction(session, "ensure param task")
    existing = await session.scalar(
        select(EtlTask.id)
        .where(EtlTask.endpoint == endpoint)
        .where(EtlTask.params.contains(params))
        .where(EtlTask.cursor_kind.is_(None))
    )
    if existing is None:
        session.add(EtlTask(endpoint=endpoint, params=params, status="pending"))


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
        .where(_due_clause(datetime.now(UTC)))
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
