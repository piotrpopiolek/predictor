"""etl_tasks queue, cursors, and checkpoints (FR-025, FR-026, FR-027, FR-029)."""

from __future__ import annotations

import os
import socket
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.constants import (
    CURSOR_KINDS,
    DICTIONARY_ENDPOINTS,
    ENRICHABLE_FIXTURE_STATUSES,
    ETL_STATUSES,
    FINISHED_FIXTURE_STATUSES,
    GLOBAL_ENDPOINT_ORDER,
    LOOKUP_WITHOUT_HTTP,
    ODDS_MAPPING_REFRESH_SECONDS,
    ODDS_MAPPING_RETRY_BACKOFF_SECONDS,
    STALE_GLOBAL_ENDPOINTS,
    TOP_PLAYER_ENDPOINTS,
    URGENT_PREMATCH_HORIZON_HOURS,
)
from predictor.models.etl import EtlRun, EtlTask
from predictor.models.fixtures import Fixture
from predictor.schemas.settings import Settings
from predictor.services.lock import WriterLock
from predictor.telemetry import start_span

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


def _mark_claimed(task: EtlTask) -> EtlTask:
    now = datetime.now(UTC)
    task.status = "in_progress"
    task.started_at = now
    task.updated_at = now
    task.attempt_count = task.attempt_count + 1
    return task


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


async def enqueue_fixture_followups(
    session: AsyncSession, extra: dict[str, Any]
) -> None:
    _require_transaction(session, "enqueue fixture followups")
    discovered = extra.get("discovered")
    if isinstance(discovered, list) and discovered:
        for row in discovered:
            if not isinstance(row, dict):
                continue
            try:
                fixture_id = int(row["id"])
                home_id = int(row["home"])
                away_id = int(row["away"])
            except (KeyError, TypeError, ValueError):
                continue
            await ensure_enrichment_task(session, fixture_id)
            await ensure_h2h_task(session, home_id, away_id)
            await ensure_predictions_task(session, fixture_id)
            await ensure_odds_task(session, fixture_id)
            league_id = row.get("league")
            season = row.get("season")
            if league_id is not None and season is not None:
                await enqueue_global_for_match(
                    session,
                    league_id=int(league_id),
                    season=int(season),
                    team_ids=(home_id, away_id),
                )
    else:
        for raw_id in extra.get("fixture_ids", []):
            fixture_id = int(raw_id)
            await ensure_enrichment_task(session, fixture_id)
            await ensure_odds_task(session, fixture_id)
    for pair in extra.get("league_seasons", []):
        await ensure_rounds_task(session, int(pair["league"]), int(pair["season"]))
        await ensure_injuries_task(session, int(pair["league"]), int(pair["season"]))


async def ensure_prematch_for_fixture_ids(
    session: AsyncSession, fixture_ids: list[int]
) -> None:
    _require_transaction(session, "ensure prematch for fixtures")
    ids = sorted({int(fid) for fid in fixture_ids})
    if not ids:
        return
    rows = await session.execute(
        select(Fixture.id, Fixture.home_team_id, Fixture.away_team_id).where(
            Fixture.id.in_(ids)
        )
    )
    for fixture_id, home_id, away_id in rows:
        await ensure_enrichment_task(session, int(fixture_id))
        await ensure_h2h_task(session, int(home_id), int(away_id))
        await ensure_predictions_task(session, int(fixture_id))
        await ensure_odds_task(session, int(fixture_id))


def h2h_param(home_id: int, away_id: int) -> str:
    return f"{home_id}-{away_id}"


async def ensure_h2h_task(session: AsyncSession, home_id: int, away_id: int) -> None:
    _require_transaction(session, "ensure h2h")
    params = {"h2h": h2h_param(home_id, away_id)}
    existing = await session.scalar(
        select(EtlTask.id)
        .where(EtlTask.endpoint == "/fixtures/headtohead")
        .where(EtlTask.params.contains(params))
        .where(EtlTask.cursor_kind.is_(None))
    )
    if existing is None:
        session.add(
            EtlTask(
                endpoint="/fixtures/headtohead",
                params=params,
                status="pending",
            )
        )


async def ensure_predictions_task(session: AsyncSession, fixture_id: int) -> None:
    _require_transaction(session, "ensure predictions")
    existing = await session.scalar(
        select(EtlTask.id)
        .where(EtlTask.endpoint == "/predictions")
        .where(EtlTask.fixture_id == fixture_id)
        .where(EtlTask.cursor_kind.is_(None))
    )
    if existing is None:
        session.add(
            EtlTask(
                endpoint="/predictions",
                fixture_id=fixture_id,
                params={"fixture": fixture_id},
                status="pending",
            )
        )


async def ensure_odds_task(session: AsyncSession, fixture_id: int) -> None:
    _require_transaction(session, "ensure odds")
    existing = await session.scalar(
        select(EtlTask.id)
        .where(EtlTask.endpoint == "/odds")
        .where(EtlTask.fixture_id == fixture_id)
        .where(EtlTask.cursor_kind.is_(None))
    )
    if existing is None:
        session.add(
            EtlTask(
                endpoint="/odds",
                fixture_id=fixture_id,
                params={"fixture": fixture_id},
                status="pending",
            )
        )


async def _claim_endpoint(session: AsyncSession, endpoint: str) -> EtlTask | None:
    stmt = (
        select(EtlTask)
        .where(EtlTask.endpoint == endpoint)
        .where(EtlTask.status.in_(("pending", "retryable_error")))
        .where(EtlTask.cursor_kind.is_(None))
        .order_by(EtlTask.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    task = await session.scalar(stmt)
    if task is None:
        return None
    return _mark_claimed(task)


async def claim_h2h_task(session: AsyncSession) -> EtlTask | None:
    _require_transaction(session, "claim h2h")
    return await _claim_endpoint(session, "/fixtures/headtohead")


async def claim_predictions_task(session: AsyncSession) -> EtlTask | None:
    _require_transaction(session, "claim predictions")
    return await _claim_fixture_endpoint(session, "/predictions", urgent=False)


async def claim_odds_task(session: AsyncSession) -> EtlTask | None:
    _require_transaction(session, "claim odds")
    return await _claim_fixture_endpoint(session, "/odds", urgent=False)


async def claim_urgent_predictions_task(
    session: AsyncSession, *, now: datetime | None = None
) -> EtlTask | None:
    _require_transaction(session, "claim urgent predictions")
    return await _claim_fixture_endpoint(
        session, "/predictions", urgent=True, now=now
    )


async def claim_urgent_odds_task(
    session: AsyncSession, *, now: datetime | None = None
) -> EtlTask | None:
    _require_transaction(session, "claim urgent odds")
    return await _claim_fixture_endpoint(session, "/odds", urgent=True, now=now)


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


async def enqueue_global_for_match(
    session: AsyncSession,
    *,
    league_id: int,
    season: int,
    team_ids: tuple[int, ...],
) -> None:
    _require_transaction(session, "enqueue global")
    league_params = {"league": league_id, "season": season}
    await ensure_param_task(session, "/standings", league_params)
    await ensure_param_task(session, "/players", league_params)
    for endpoint in TOP_PLAYER_ENDPOINTS:
        await ensure_param_task(session, endpoint, league_params)
    for team_id in dict.fromkeys(team_ids):
        await ensure_param_task(session, "/teams", {"id": team_id})
        await ensure_param_task(session, "/players/squads", {"team": team_id})
        await ensure_param_task(
            session,
            "/teams/statistics",
            {"team": team_id, "league": league_id, "season": season},
        )


async def enqueue_player_catalog(session: AsyncSession, player_id: int) -> None:
    _require_transaction(session, "enqueue player catalog")
    params = {"player": player_id}
    await ensure_param_task(session, "/players/profiles", params)
    await ensure_param_task(session, "/players/teams", params)
    await ensure_param_task(session, "/transfers", params)
    await ensure_param_task(session, "/trophies", params)
    await ensure_param_task(session, "/sidelined", params)


async def enqueue_coach_catalog(session: AsyncSession, coach_id: int) -> None:
    _require_transaction(session, "enqueue coach catalog")
    await ensure_param_task(session, "/coachs", {"id": coach_id})
    await ensure_param_task(session, "/trophies", {"coach": coach_id})
    await ensure_param_task(session, "/sidelined", {"coach": coach_id})


async def claim_global_task(session: AsyncSession) -> EtlTask | None:
    _require_transaction(session, "claim global")
    for endpoint in GLOBAL_ENDPOINT_ORDER:
        task = await _claim_endpoint(session, endpoint)
        if task is not None:
            return task
    return None


async def requeue_stale_global(session: AsyncSession, now: datetime) -> int:
    _require_transaction(session, "requeue stale global")
    count = 0
    tasks = await session.scalars(
        select(EtlTask)
        .where(EtlTask.endpoint.in_(tuple(STALE_GLOBAL_ENDPOINTS)))
        .where(EtlTask.status == "complete")
        .where(EtlTask.cursor_kind.is_(None))
    )
    for task in tasks:
        if needs_refresh(task, now):
            task.status = "pending"
            task.updated_at = now
            task.completed_at = None
            count += 1
    return count


async def skip_reconstructable_lookups(session: AsyncSession) -> int:
    _require_transaction(session, "skip lookups")
    count = 0
    for endpoint in LOOKUP_WITHOUT_HTTP:
        while True:
            task = await _claim_endpoint(session, endpoint)
            if task is None:
                break
            await complete_task(
                session,
                task,
                "not_supported",
                error="reconstructable_lookup",
            )
            count += 1
    return count
