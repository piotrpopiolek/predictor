"""Day and match completeness (FR-028) plus the daily operator report (FR-035)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.client.quota import QuotaSnapshot
from predictor.constants import (
    DAILY_REPORT_ENDPOINT,
    DAY_CONTRACT_ENDPOINTS,
    ENRICHABLE_FIXTURE_STATUSES,
    FINISHED_FIXTURE_STATUSES,
    OPEN_ETL_STATUSES,
)
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.services.queue import TERMINAL_STATUSES, h2h_param


def _utc_day_range(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


async def day_fixtures_task(session: AsyncSession, day: date) -> EtlTask | None:
    task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.day_utc == day)
        .where(EtlTask.fixture_id.is_(None))
        .where(EtlTask.cursor_kind.is_(None))
        .order_by(EtlTask.id)
        .limit(1)
    )
    if task is None:
        return None
    return task


async def day_fixtures_fetched(session: AsyncSession, day: date) -> bool:
    task = await day_fixtures_task(session, day)
    if task is None or task.status != "complete":
        return False
    if (
        task.paging_total is not None
        and task.paging_current is not None
        and task.paging_current < task.paging_total
    ):
        return False
    return True


async def fixtures_on_utc_day(session: AsyncSession, day: date) -> list[Fixture]:
    start, end = _utc_day_range(day)
    result = await session.scalars(
        select(Fixture)
        .where(Fixture.date.is_not(None))
        .where(Fixture.date >= start)
        .where(Fixture.date < end)
        .order_by(Fixture.id)
    )
    return list(result)


async def match_blockers(session: AsyncSession, fixture: Fixture) -> list[str]:
    status = fixture.status_short
    if status not in ENRICHABLE_FIXTURE_STATUSES:
        return []
    blockers: list[str] = []
    enrichment = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.fixture_id == fixture.id)
        .where(EtlTask.cursor_kind.is_(None))
        .order_by(EtlTask.id)
        .limit(1)
    )
    if enrichment is None or enrichment.status in OPEN_ETL_STATUSES:
        blockers.append("/fixtures")
    if status in FINISHED_FIXTURE_STATUSES:
        half = await session.scalar(
            select(EtlTask)
            .where(EtlTask.endpoint == "/fixtures/statistics")
            .where(EtlTask.fixture_id == fixture.id)
            .where(EtlTask.cursor_kind.is_(None))
            .order_by(EtlTask.id)
            .limit(1)
        )
        if half is not None and half.status in OPEN_ETL_STATUSES:
            blockers.append("/fixtures/statistics")
    for endpoint in ("/predictions", "/odds"):
        child = await session.scalar(
            select(EtlTask)
            .where(EtlTask.endpoint == endpoint)
            .where(EtlTask.fixture_id == fixture.id)
            .where(EtlTask.cursor_kind.is_(None))
            .order_by(EtlTask.id)
            .limit(1)
        )
        if child is not None and child.status in OPEN_ETL_STATUSES:
            blockers.append(endpoint)
    pair = h2h_param(fixture.home_team_id, fixture.away_team_id)
    h2h = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures/headtohead")
        .where(EtlTask.params.contains({"h2h": pair}))
        .where(EtlTask.cursor_kind.is_(None))
        .order_by(EtlTask.id)
        .limit(1)
    )
    if h2h is not None and h2h.status in OPEN_ETL_STATUSES:
        blockers.append("/fixtures/headtohead")
    return blockers


async def match_is_complete(session: AsyncSession, fixture: Fixture) -> bool:
    return not await match_blockers(session, fixture)


async def day_is_complete(session: AsyncSession, day: date) -> bool:
    if not await day_fixtures_fetched(session, day):
        return False
    fixtures = await fixtures_on_utc_day(session, day)
    for fixture in fixtures:
        if await match_blockers(session, fixture):
            return False
    return True


async def write_daily_report(
    session: AsyncSession,
    day: date,
    *,
    quota: QuotaSnapshot | None = None,
    now: datetime | None = None,
) -> bool:
    existing = await session.scalar(
        select(EtlTask.id)
        .where(EtlTask.endpoint == DAILY_REPORT_ENDPOINT)
        .where(EtlTask.day_utc == day)
        .where(EtlTask.cursor_kind.is_(None))
    )
    if existing is not None:
        return False
    start, end = _utc_day_range(day)
    status_rows = await session.execute(
        select(EtlTask.status, func.count())
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.endpoint != DAILY_REPORT_ENDPOINT)
        .group_by(EtlTask.status)
    )
    task_counts = {str(status): int(count) for status, count in status_rows}
    finished_rows = await session.execute(
        select(EtlTask.endpoint, EtlTask.status, func.count())
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.endpoint != DAILY_REPORT_ENDPOINT)
        .where(EtlTask.completed_at.is_not(None))
        .where(EtlTask.completed_at >= start)
        .where(EtlTask.completed_at < end)
        .group_by(EtlTask.endpoint, EtlTask.status)
    )
    completed: list[dict[str, Any]] = [
        {"endpoint": endpoint, "status": status, "count": int(count)}
        for endpoint, status, count in finished_rows
    ]
    backfill = await session.scalar(
        select(EtlTask).where(EtlTask.cursor_kind == "backfill").limit(1)
    )
    backfill_params = dict(backfill.params) if backfill is not None else {}
    live = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == "/odds/live")
        .where(EtlTask.cursor_kind.is_(None))
        .order_by(EtlTask.id)
        .limit(1)
    )
    error_tasks = await session.scalars(
        select(EtlTask)
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.status.in_(("retryable_error", "permanent_error")))
        .where(EtlTask.endpoint != DAILY_REPORT_ENDPOINT)
        .order_by(EtlTask.updated_at.desc())
        .limit(20)
    )
    errors = [
        {
            "endpoint": task.endpoint,
            "status": task.status,
            "error": task.last_error,
        }
        for task in error_tasks
    ]
    complete = await day_is_complete(session, day)
    quota_block: dict[str, Any] | None = None
    if quota is not None:
        quota_block = {
            "current": quota.current,
            "limit_day": quota.limit_day,
            "remaining": quota.remaining,
            "source": quota.source,
        }
    params: dict[str, Any] = {
        "day": day.isoformat(),
        "complete": complete,
        "task_counts": task_counts,
        "completed_by_endpoint": completed,
        "backfill_last_complete": backfill_params.get("last_complete"),
        "backfill_next_day": backfill_params.get("next_day"),
        "live_last_poll_at": (
            live.params.get("last_poll_at") if live is not None else None
        ),
        "errors": errors,
        "quota": quota_block,
        "open_contract_endpoints": sorted(DAY_CONTRACT_ENDPOINTS),
        "terminal_statuses": sorted(TERMINAL_STATUSES),
    }
    stamp = now or datetime.now(UTC)
    session.add(
        EtlTask(
            endpoint=DAILY_REPORT_ENDPOINT,
            params=params,
            day_utc=day,
            status="complete",
            completed_at=stamp,
        )
    )
    return True
