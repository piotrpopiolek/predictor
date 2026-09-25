"""Quota budget: keep enough requests for live matches until UTC midnight.

History (priorities 5–9) and live odds spend only the surplus above that
reserve. Match details slow from 5 to 15 minutes when the reserve is tight.
An empty board polls scores every 5 minutes instead of every minute.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from predictor.client.quota import QuotaSnapshot
from predictor.constants import (
    IDLE_SCORE_POLL_SECONDS,
    LIVE_CONTEXT_ONESHOT_CALLS,
    LIVE_DETAIL_REFRESH_MAX_SECONDS,
    LIVE_DETAIL_REFRESH_SECONDS,
    LIVE_REQUESTS_PER_TICK,
    MATCH_LIVE_HOURS,
    QUOTA_SNAPSHOT_ENDPOINT,
)
from predictor.models.etl import EtlTask


@dataclass(frozen=True, slots=True)
class LiveBudgetPlan:
    """Requests to hold until reset, and how fast live work may run."""

    score_need: int
    detail_need: int
    odds_need: int
    detail_refresh_seconds: int
    oneshot_calls: int
    score_poll_seconds: float
    poll_odds: bool

    @property
    def full_reserve(self) -> int:
        return self.score_need + self.detail_need + self.odds_need


@dataclass
class LiveSpendGate:
    """Mutable view of the latest plan, read by the live-context handler."""

    detail_refresh_seconds: int = LIVE_DETAIL_REFRESH_SECONDS
    oneshot_calls: int = LIVE_CONTEXT_ONESHOT_CALLS


def plan_live_budget(
    *,
    live_matches: int,
    remaining: int,
    now: datetime,
    target_seconds: int = 60,
    matches_left_today: int | None = None,
) -> LiveBudgetPlan:
    """Reserve calls for matches in play and those still to be played today.

    ``live_matches`` is the board right now. ``matches_left_today`` also
    counts kickoffs later today that have not finished. Details are reserved
    per match for ``MATCH_LIVE_HOURS``, not as if every one of them stayed
    live until midnight. Shared score and odds polls are reserved until
    reset whenever any such match remains. The score poll itself stays on
    the slow idle cadence until something is actually in play.
    """
    hours = seconds_until_utc_midnight(now) / 3600.0
    covered = max(0, live_matches)
    if matches_left_today is not None:
        covered = max(covered, matches_left_today)
    in_play = live_matches > 0
    score_target = target_seconds if covered > 0 else IDLE_SCORE_POLL_SECONDS
    score_per_hour = 3600.0 / score_target
    score_need = math.ceil(hours * score_per_hour)
    detail_need = 0
    if covered > 0:
        detail_need = math.ceil(
            covered * MATCH_LIVE_HOURS * (3600.0 / LIVE_DETAIL_REFRESH_SECONDS)
        )
    odds_need = 0 if covered == 0 else math.ceil(hours * (3600.0 / target_seconds))
    score_poll = _score_poll_seconds(
        remaining=remaining,
        score_need=score_need if in_play or covered == 0 else 0,
        target_seconds=target_seconds if in_play else IDLE_SCORE_POLL_SECONDS,
        now=now,
    )
    return LiveBudgetPlan(
        score_need=score_need,
        detail_need=detail_need,
        odds_need=odds_need,
        detail_refresh_seconds=_detail_refresh_seconds(
            covered=covered,
            in_play=in_play,
            remaining=remaining,
            score_need=score_need,
            detail_need=detail_need,
        ),
        oneshot_calls=(
            LIVE_CONTEXT_ONESHOT_CALLS if in_play and remaining > score_need else 0
        ),
        score_poll_seconds=score_poll,
        poll_odds=in_play and odds_need > 0,
    )


def quota_allows(priority: int, snapshot: QuotaSnapshot, plan: LiveBudgetPlan) -> bool:
    """History and odds run only above the live reserve. Scores always run."""
    remaining = snapshot.remaining
    if remaining <= 0:
        return False
    if priority <= 2:
        return True
    if remaining <= LIVE_REQUESTS_PER_TICK:
        return False
    if priority == 3:
        return remaining > plan.score_need
    if priority == 4:
        return plan.poll_odds and remaining > plan.score_need + plan.detail_need
    return remaining > plan.full_reserve


def _score_poll_seconds(
    *,
    remaining: int,
    score_need: int,
    target_seconds: int,
    now: datetime,
) -> float:
    target = float(target_seconds)
    if remaining >= score_need and remaining > 0:
        return target
    if remaining <= 0:
        return seconds_until_utc_midnight(now)
    ticks = max(1, remaining)
    return max(target, seconds_until_utc_midnight(now) / ticks)


def _detail_refresh_seconds(
    *,
    covered: int,
    in_play: bool,
    remaining: int,
    score_need: int,
    detail_need: int,
) -> int:
    slowest = LIVE_DETAIL_REFRESH_MAX_SECONDS
    fastest = LIVE_DETAIL_REFRESH_SECONDS
    if not in_play or covered <= 0:
        return fastest
    budget = remaining - score_need
    if budget >= detail_need:
        return fastest
    if budget <= 0:
        return slowest
    raw = (covered * MATCH_LIVE_HOURS * 3600.0) / budget
    return int(min(slowest, max(fastest, math.ceil(raw))))


def seconds_until_utc_midnight(now: datetime) -> float:
    """Seconds until the vendor daily quota resets."""
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


def live_poll_interval_gauge(
    remaining: int,
    used: int,
    limit_day: int,
    target_seconds: int,
    now: datetime,
) -> float:
    """Interval Prometheus should compare snapshot age against (FR-019)."""
    if remaining < 0:
        return float(target_seconds)
    return live_poll_interval_seconds(
        QuotaSnapshot(
            current=used,
            limit_day=max(limit_day, 1),
            remaining=remaining,
            source="api",
        ),
        target_seconds,
        now,
    )


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
    session: AsyncSession,
    snapshot: QuotaSnapshot,
    *,
    score_poll_seconds: float | None = None,
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
    if score_poll_seconds is not None:
        params["score_poll_seconds"] = score_poll_seconds
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
