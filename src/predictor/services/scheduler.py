"""Runtime scheduler (FR-023). W2: quota loop and empty domain slots."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.client.errors import (
    AuthBlockedError,
    FootballHttpError,
    QuotaExhaustedError,
    RetryableHttpError,
)
from predictor.client.football import FootballClient
from predictor.client.quota import QuotaSnapshot
from predictor.logutil import log_json
from predictor.schemas.settings import Settings
from predictor.services.completeness import write_daily_report
from predictor.services.queue import ensure_cursors
from predictor.services.quota import (
    LiveBudgetPlan,
    LiveSpendGate,
    persist_quota_snapshot,
    plan_live_budget,
    quota_allows,
    seconds_until_utc_midnight,
)

Handler = Callable[[], Awaitable[None]]


class PrioritySlot(NamedTuple):
    priority: int
    name: str


PRIORITY_ORDER: tuple[PrioritySlot, ...] = (
    PrioritySlot(1, "quota_and_live_dictionaries"),
    PrioritySlot(2, "live_fixtures"),
    PrioritySlot(3, "live_context"),
    PrioritySlot(4, "odds_live"),
    PrioritySlot(5, "finalization"),
    PrioritySlot(6, "forward_sync"),
    PrioritySlot(7, "enrichment"),
    PrioritySlot(8, "backfill"),
    PrioritySlot(9, "global_entities"),
)

STATUS_REFRESH_SECONDS = 3600.0


async def _noop() -> None:
    return None


def _blocks_until_reset(snapshot: QuotaSnapshot, now: datetime) -> bool:
    if snapshot.source != "api" or snapshot.remaining > 0:
        return False
    fetched = snapshot.fetched_at
    if fetched is None:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    return fetched.astimezone(UTC).date() == now.astimezone(UTC).date()


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        client: FootballClient,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        handlers: Mapping[int, Handler] | None = None,
        idle_cap_seconds: float = 60.0,
        status_refresh_seconds: float = STATUS_REFRESH_SECONDS,
        now_fn: Callable[[], datetime] | None = None,
        live_match_count: Callable[[], int] | None = None,
        matches_left_today: Callable[[], Awaitable[int]] | None = None,
        gate: LiveSpendGate | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._session_factory = session_factory
        self._handlers = dict(handlers or {})
        self._idle_cap_seconds = idle_cap_seconds
        self._status_refresh_seconds = status_refresh_seconds
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._live_match_count = live_match_count or (lambda: 0)
        self._matches_left_today = matches_left_today
        self._left_today: int | None = None
        self._gate = gate or LiveSpendGate()
        self._quota: QuotaSnapshot | None = None
        self._live_interval_seconds: float | None = None

    @property
    def quota(self) -> QuotaSnapshot | None:
        return self._quota

    @property
    def live_interval_seconds(self) -> float | None:
        return self._live_interval_seconds

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._tick(stop)
            timeout = self._sleep_seconds()
            try:
                await asyncio.wait_for(stop.wait(), timeout=timeout)
            except TimeoutError:
                continue

    async def _tick(self, stop: asyncio.Event) -> None:
        if stop.is_set():
            return
        try:
            await self._maybe_daily_report()
            quota = await self._ensure_quota()
            self._quota = quota
            if quota.source == "api" and quota.remaining <= 0:
                await self._persist_cursors()
                log_json(
                    logging.INFO,
                    service="worker",
                    event="quota_exhausted",
                    remaining=0,
                )
                return
            await self._refresh_matches_left()
            target = float(self._settings.live_poll_target_seconds)
            plan = self._apply_budget(quota.remaining)
            if plan.score_poll_seconds > target and self._live_match_count() > 0:
                log_json(
                    logging.WARNING,
                    service="worker",
                    event="live_freshness_missed",
                    interval_seconds=round(plan.score_poll_seconds, 3),
                    target_seconds=self._settings.live_poll_target_seconds,
                    remaining=quota.remaining,
                )
            tick_start = self._now()
            for slot in PRIORITY_ORDER:
                if stop.is_set():
                    return
                plan = self._apply_budget(quota.remaining)
                if not quota_allows(slot.priority, quota, plan):
                    continue
                # P1–P3 always (dictionaries, live scores, live context).
                # P4+ (odds/live, then residual) stop once the tick hits target.
                if slot.priority >= 4:
                    elapsed = (self._now() - tick_start).total_seconds()
                    if elapsed >= target:
                        break
                handler = self._handlers.get(slot.priority, _noop)
                try:
                    await handler()
                except QuotaExhaustedError:
                    fresh = self._client_quota()
                    if fresh is not None:
                        self._quota = fresh
                    break
        finally:
            await self._persist_quota()

    async def _persist_quota(self) -> None:
        snapshot = getattr(self._client, "quota", None)
        if snapshot is None:
            snapshot = self._quota
        if snapshot is None or snapshot.source != "api":
            return
        try:
            session_cm = self._session_factory()
        except TypeError:
            return
        try:
            async with session_cm as session:
                async with session.begin():
                    await persist_quota_snapshot(
                        session,
                        snapshot,
                        score_poll_seconds=self._live_interval_seconds,
                    )
        except (TypeError, AttributeError):
            return
        except Exception:
            log_json(logging.WARNING, service="worker", event="quota_persist_failed")

    async def _maybe_daily_report(self) -> None:
        yesterday = self._now().astimezone(UTC).date() - timedelta(days=1)
        wrote = False
        try:
            session_cm = self._session_factory()
        except TypeError:
            return
        try:
            async with session_cm as session:
                async with session.begin():
                    wrote = await write_daily_report(
                        session,
                        yesterday,
                        quota=self._quota,
                        now=self._now(),
                    )
        except (TypeError, AttributeError):
            return
        if wrote:
            log_json(
                logging.INFO,
                service="worker",
                event="daily_report",
                day=yesterday.isoformat(),
            )

    async def _refresh_matches_left(self) -> None:
        if self._matches_left_today is None:
            self._left_today = None
            return
        try:
            self._left_today = max(0, int(await self._matches_left_today()))
        except Exception:
            log_json(
                logging.WARNING,
                service="worker",
                event="matches_left_today_failed",
            )
            self._left_today = None

    def _apply_budget(self, remaining: int) -> LiveBudgetPlan:
        plan = plan_live_budget(
            live_matches=max(0, self._live_match_count()),
            matches_left_today=self._left_today,
            remaining=remaining,
            now=self._now(),
            target_seconds=int(self._settings.live_poll_target_seconds),
        )
        self._gate.detail_refresh_seconds = plan.detail_refresh_seconds
        self._gate.oneshot_calls = plan.oneshot_calls
        self._live_interval_seconds = plan.score_poll_seconds
        return plan

    def _client_quota(self) -> QuotaSnapshot | None:
        snapshot = getattr(self._client, "quota", None)
        if isinstance(snapshot, QuotaSnapshot):
            return snapshot
        return None

    async def _ensure_quota(self) -> QuotaSnapshot:
        now = self._now()
        observed = self._client_quota()
        if observed is not None and _blocks_until_reset(observed, now):
            return observed
        cached = self._quota
        if cached is not None and cached.source == "api":
            if cached.remaining <= 0 and cached.fetched_at is not None:
                if (
                    cached.fetched_at.astimezone(UTC).date()
                    == now.astimezone(UTC).date()
                ):
                    return cached
            elif cached.fetched_at is not None:
                age = (now - cached.fetched_at).total_seconds()
                if age < self._status_refresh_seconds:
                    return cached
        try:
            snapshot = await self._client.get_status()
        except AuthBlockedError:
            raise
        except QuotaExhaustedError:
            observed = self._client_quota()
            if observed is not None and observed.remaining <= 0:
                return observed
            return QuotaSnapshot(
                current=self._settings.quota_daily_limit,
                limit_day=self._settings.quota_daily_limit,
                remaining=0,
                fetched_at=now,
                source="api",
            )
        except (RetryableHttpError, FootballHttpError):
            log_json(logging.WARNING, service="worker", event="quota_refresh_failed")
            if cached is not None:
                return cached
            return QuotaSnapshot(
                current=0,
                limit_day=self._settings.quota_daily_limit,
                remaining=0,
                fetched_at=now,
                source="unknown",
            )
        return snapshot

    async def _persist_cursors(self) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await ensure_cursors(session)

    def _sleep_seconds(self) -> float:
        quota = self._quota
        if quota is not None and quota.source == "api" and quota.remaining <= 0:
            until_reset = seconds_until_utc_midnight(self._now())
            return min(self._idle_cap_seconds, until_reset)
        if self._live_interval_seconds is not None:
            return self._live_interval_seconds
        return float(self._settings.live_poll_target_seconds)
