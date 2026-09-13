"""Quota budget: live priorities may use the 3% buffer; the rest may not."""

from __future__ import annotations

import math
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from predictor.client.quota import QuotaSnapshot
from predictor.constants import (
    LIVE_QUOTA_PRIORITIES,
    LIVE_REQUESTS_PER_TICK,
    QUOTA_SNAPSHOT_ENDPOINT,
)
from predictor.models.etl import EtlTask


def quota_allows(priority: int, snapshot: QuotaSnapshot, buffer_percent: float) -> bool:
    if snapshot.remaining <= 0:
        return False
    if priority in LIVE_QUOTA_PRIORITIES:
        return True
    floor = math.ceil(snapshot.limit_day * (buffer_percent / 100.0))
    return snapshot.remaining > floor


def seconds_until_utc_midnight(now: datetime) -> float:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    utc_now = now.astimezone(UTC)
    nxt = datetime.combine(utc_now.date() + timedelta(days=1), time.min, tzinfo=UTC)
    return max(1.0, (nxt - utc_now).total_seconds())


def live_poll_interval_seconds(
    snapshot: QuotaSnapshot,
    target_seconds: int,
    now: datetime,
    *,
    requests_per_tick: int = LIVE_REQUESTS_PER_TICK,
) -> float:
    """Return the sleep between live ticks. Stretch past the target when quota
    cannot sustain it until UTC midnight (FR-019)."""
    target = float(target_seconds)
    if snapshot.remaining <= 0:
        return seconds_until_utc_midnight(now)
    cost = max(1, requests_per_tick)
    seconds_left = seconds_until_utc_midnight(now)
    projected = (seconds_left / target) * cost
    if projected <= snapshot.remaining:
        return target
    max_ticks = snapshot.remaining // cost
    if max_ticks <= 0:
        return seconds_until_utc_midnight(now)
    return max(target, seconds_left / max_ticks)


def quota_gauges_from_params(params: dict[str, Any] | None) -> tuple[int, int]:
    """Return (remaining, used). remaining is -1 when unknown."""
    if not params:
        return -1, 0
    try:
        remaining = int(params["remaining"])
        used = int(params.get("current", 0))
    except (KeyError, TypeError, ValueError):
        return -1, 0
    return remaining, used


async def persist_quota_snapshot(
    session: AsyncSession, snapshot: QuotaSnapshot
) -> None:
    if snapshot.source != "api":
        return
    params = {
        "current": snapshot.current,
        "limit_day": snapshot.limit_day,
        "remaining": snapshot.remaining,
        "source": snapshot.source,
        "fetched_at": (
            snapshot.fetched_at.isoformat() if snapshot.fetched_at is not None else None
        ),
    }
    task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == QUOTA_SNAPSHOT_ENDPOINT)
        .order_by(EtlTask.id)
        .limit(1)
    )
    now = datetime.now(UTC)
    if task is None:
        session.add(
            EtlTask(
                endpoint=QUOTA_SNAPSHOT_ENDPOINT,
                params=params,
                status="complete",
                completed_at=now,
            )
        )
        return
    task.params = params
    flag_modified(task, "params")
    task.status = "complete"
    task.updated_at = now
