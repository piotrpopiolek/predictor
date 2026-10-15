"""Runtime scheduler (FR-023). W2: quota loop and empty domain slots."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.client.errors import (
    AuthBlockedError,
    FootballHttpError,
    QuotaExhaustedError,
    RetryableHttpError,
    SpendWindowClosed,
)
from predictor.client.football import FootballClient
from predictor.client.quota import QuotaSnapshot
from predictor.logutil import log_json
from predictor.schemas.settings import Settings
from predictor.services.completeness import write_daily_report
from predictor.services.queue import ensure_cursors
from predictor.services.quota import (
    ContextCredit,
    DayLoadForecast,
    KickoffLoad,
    LiveBudgetPlan,
    LiveSpendGate,
    context_allowance,
    history_call_allowance,
    persist_quota_snapshot,
    plan_live_budget,
    queue_cycle_seconds,
    quota_allows,
    refund_context_credit,
    safety_buffer_requests,
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
RESET_PROBE_SECONDS = 15 * 60


async def _noop() -> None:
    return None


def _blocks_until_reset(snapshot: QuotaSnapshot, now: datetime) -> bool:
    if snapshot.source != "api" or snapshot.remaining > 0:
        return False
    return _is_same_utc_day(snapshot, now)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _is_same_utc_day(snapshot: QuotaSnapshot, now: datetime) -> bool:
    fetched = snapshot.fetched_at
    if fetched is None:
        return False
    return _as_utc(fetched).date() == _as_utc(now).date()


def _seconds_since_utc_midnight(now: datetime) -> float:
    utc = _as_utc(now)
    midnight = utc.replace(hour=0, minute=0, second=0, microsecond=0)
    return (utc - midnight).total_seconds()


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
        matches_left_today: (
            Callable[[], Awaitable[int | DayLoadForecast]] | None
        ) = None,
        finishing_match_count: Callable[[], int] | None = None,
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
        self._finishing_match_count = finishing_match_count or (lambda: 0)
        self._left_today: int | None = None
        self._forecast: DayLoadForecast | None = None
        self._gate = gate or LiveSpendGate()
        self._credit = ContextCredit()
        self._quota: QuotaSnapshot | None = None
        self._status_quota: QuotaSnapshot | None = None
        self._calls_baseline = 0
        self._plan: LiveBudgetPlan | None = None
        self._live_interval_seconds: float | None = None

    @property
    def quota(self) -> QuotaSnapshot | None:
        self._work_started = False
        return self._quota

    @property
    def live_interval_seconds(self) -> float | None:
        return self._live_interval_seconds

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._tick(stop)
            await self._drain_during_sleep(stop, self._sleep_seconds())

    async def _drain_during_sleep(self, stop: asyncio.Event, window: float) -> None:
        """Spend the history call pool inside the sleep that already follows a tick.

        The next score poll still starts when this window ends. A slow API
        stops early and leaves the unused calls for the next window. The
        vendor minute cap stops a burst.
        """
        started = time.monotonic()
        deadline = started + window
        try:
            if window > 0 and self._work_started and not stop.is_set():
                await self._spend_history(stop, deadline, window)
        finally:
            self._close_history_window()
            leftover = deadline - time.monotonic()
            if leftover > 0 and not stop.is_set():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=leftover)

    async def _spend_history(
        self,
        stop: asyncio.Event,
        deadline: float,
        window: float,
    ) -> None:
        quota = self._quota
        plan = self._plan
        if quota is None or plan is None or quota.source != "api" or plan.overloaded:
            return
        surplus = quota.remaining - plan.safety_buffer - plan.full_reserve
        allowance = history_call_allowance(
            surplus=surplus,
            seconds_left=seconds_until_utc_midnight(self._now()),
            cycle_seconds=queue_cycle_seconds(
                window,
                float(self._settings.live_poll_target_seconds),
            ),
        )
        if allowance <= 0:
            return
        baseline = self._client_calls()
        self._open_history_window(deadline, baseline + allowance)
        once_done: set[int] = set()
        while not stop.is_set() and time.monotonic() < deadline:
            if self._client_calls() - baseline >= allowance or self._minute_full():
                break
            made = await self._one_history_pass(
                stop,
                deadline,
                baseline,
                allowance,
                once_done,
            )
            if not made:
                break
        log_json(
            logging.INFO,
            service="worker",
            event="history_window",
            allowance=allowance,
            spent=self._client_calls() - baseline,
        )

    async def _one_history_pass(
        self,
        stop: asyncio.Event,
        deadline: float,
        baseline: int,
        allowance: int,
        once_done: set[int],
    ) -> bool:
        before = self._client_calls()
        for slot in PRIORITY_ORDER:
            if slot.priority < 5:
                continue
            # Forward sync refetches today on every call. Once per window.
            if slot.priority == 6 and 6 in once_done:
                continue
            if (
                stop.is_set()
                or time.monotonic() >= deadline
                or self._client_calls() - baseline >= allowance
                or self._minute_full()
            ):
                break
            quota = self._quota
            if quota is None:
                break
            quota = self._prefer_observed(quota)
            self._quota = quota
            plan = self._apply_budget(quota)
            if not quota_allows(slot.priority, quota, plan):
                continue
            handler = self._handlers.get(slot.priority)
            if handler is None:
                continue
            if slot.priority == 6:
                once_done.add(6)
            try:
                await handler()
            except SpendWindowClosed:
                break
            except QuotaExhaustedError:
                fresh = self._client_quota()
                if fresh is not None:
                    self._quota = fresh
                return False
        return self._client_calls() > before

    def _open_history_window(self, deadline: float, call_ceiling: int) -> None:
        opener = getattr(self._client, "open_history_window", None)
        if opener is None:
            return
        opener(
            deadline=deadline,
            call_ceiling=call_ceiling,
            per_minute=self._settings.quota_requests_per_minute,
        )

    def _close_history_window(self) -> None:
        closer = getattr(self._client, "close_history_window", None)
        if closer is not None:
            closer()

    def _minute_full(self) -> bool:
        recent = getattr(self._client, "calls_in_recent_seconds", None)
        if recent is None:
            return False
        return int(recent(60.0)) >= self._settings.quota_requests_per_minute

    async def _tick(self, stop: asyncio.Event) -> None:
        self._work_started = False
        if stop.is_set():
            return
        try:
            await self._maybe_daily_report()
            quota = await self._ensure_quota()
            self._calls_baseline = self._client_calls()
            quota = self._prefer_observed(quota)
            self._quota = quota
            if self._probing_reset():
                self._apply_budget(self._status_quota or quota)
                self._live_interval_seconds = min(
                    self._idle_cap_seconds,
                    seconds_until_utc_midnight(self._now()),
                )
                return
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
            plan = self._apply_budget(quota)
            if self._status_confirms_buffer():
                self._warn_if_live_stretched(plan, target, quota)
                return
            self._warn_if_live_stretched(plan, target, quota)
            self._work_started = True
            tick_start = self._now()
            for slot in PRIORITY_ORDER:
                if stop.is_set():
                    return
                quota = self._prefer_observed(quota)
                self._quota = quota
                plan = self._apply_budget(quota)
                if not quota_allows(slot.priority, quota, plan):
                    continue
                # P1–P3 always (dictionaries, live scores, live context).
                # P4+ (odds/live, then residual) stop once the tick hits target.
                if slot.priority >= 4:
                    elapsed = (self._now() - tick_start).total_seconds()
                    if elapsed >= target:
                        break
                allowance = 0
                if slot.priority == 3:
                    room = quota.remaining - plan.safety_buffer - 1
                    allowance = context_allowance(
                        self._credit,
                        plan.context_calls_per_tick,
                        room=max(0, room),
                    )
                    self._gate.max_context_calls = allowance
                    self._gate.spent_calls = 0
                    if allowance <= 0:
                        continue
                handler = self._handlers.get(slot.priority, _noop)
                try:
                    await handler()
                except QuotaExhaustedError:
                    fresh = self._client_quota()
                    if fresh is not None:
                        self._quota = fresh
                    break
                finally:
                    if slot.priority == 3 and allowance > 0:
                        unused = allowance - max(0, self._gate.spent_calls)
                        refund_context_credit(
                            self._credit,
                            unused,
                            plan.context_calls_per_tick,
                        )
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
            async with session_cm as session, session.begin():
                plan = self._plan
                await persist_quota_snapshot(
                    session,
                    snapshot,
                    score_poll_seconds=self._live_interval_seconds,
                    detail_refresh_seconds=(
                        None if plan is None else plan.relaxed_refresh_seconds
                    ),
                    context_call_cap=(
                        None if plan is None else plan.context_calls_per_tick
                    ),
                    overloaded=None if plan is None else plan.overloaded,
                    allowed_per_minute=(
                        None if plan is None else plan.allowed_per_minute
                    ),
                )
        except TypeError, AttributeError:
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
            async with session_cm as session, session.begin():
                wrote = await write_daily_report(
                    session,
                    yesterday,
                    quota=self._quota,
                    now=self._now(),
                )
        except TypeError, AttributeError:
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
            self._forecast = None
            return
        try:
            raw = await self._matches_left_today()
        except Exception:
            log_json(
                logging.WARNING,
                service="worker",
                event="matches_left_today_failed",
            )
            self._left_today = None
            self._forecast = None
            return
        if isinstance(raw, DayLoadForecast):
            self._forecast = raw
            self._left_today = raw.matches
            return
        count = max(0, int(raw))
        self._left_today = count
        self._forecast = DayLoadForecast(
            kickoffs=(KickoffLoad(kickoff=self._now(), count=count),) if count else ()
        )

    def _warn_if_live_stretched(
        self,
        plan: LiveBudgetPlan,
        target: float,
        quota: QuotaSnapshot,
    ) -> None:
        if plan.score_poll_seconds > target and self._live_match_count() > 0:
            log_json(
                logging.WARNING,
                service="worker",
                event="live_freshness_missed",
                interval_seconds=round(plan.score_poll_seconds, 3),
                target_seconds=self._settings.live_poll_target_seconds,
                remaining=quota.remaining,
            )

    def _apply_budget(self, snapshot: QuotaSnapshot) -> LiveBudgetPlan:
        limit = snapshot.limit_day or self._settings.quota_daily_limit
        plan = plan_live_budget(
            live_matches=max(0, self._live_match_count()),
            finishing_matches=max(0, self._finishing_match_count()),
            matches_left_today=None,
            forecast=self._forecast,
            remaining=max(0, snapshot.remaining),
            now=self._now(),
            target_seconds=int(self._settings.live_poll_target_seconds),
            limit_day=limit,
            buffer_percent=self._settings.quota_safety_buffer_percent,
        )
        self._plan = plan
        self._gate.detail_refresh_seconds = plan.detail_refresh_seconds
        self._gate.relaxed_refresh_seconds = plan.relaxed_refresh_seconds
        self._gate.oneshot_calls = plan.oneshot_calls
        self._gate.odds_refresh_seconds = plan.odds_refresh_seconds
        self._gate.max_context_calls = plan.max_context_calls
        self._live_interval_seconds = plan.score_poll_seconds
        return plan

    def _prefer_observed(self, quota: QuotaSnapshot) -> QuotaSnapshot:
        """Plan from response headers once they are newer than ``/status``.

        A drop larger than the calls made in this tick is the previous day's
        counter still sitting on the header. Keep the ``/status`` reading.
        """
        observed = self._client_quota()
        if observed is None or observed.source != "api":
            return quota
        now = self._now()
        fetched = observed.fetched_at
        if fetched is not None and not _is_same_utc_day(observed, now):
            return quota
        current = quota.fetched_at
        if fetched is not None and current is not None and fetched < current:
            return quota
        drop = quota.remaining - observed.remaining
        if drop > self._calls_this_tick():
            return quota
        return observed

    def _client_quota(self) -> QuotaSnapshot | None:
        snapshot = getattr(self._client, "quota", None)
        if isinstance(snapshot, QuotaSnapshot):
            return snapshot
        return None

    async def _ensure_quota(self) -> QuotaSnapshot:
        now = self._now()
        if self._probing_reset():
            return await self._fetch_status(now)
        observed = self._client_quota()
        if observed is not None and _blocks_until_reset(observed, now):
            self._remember_status(observed)
            return observed
        cached = self._quota
        if (
            cached is not None
            and cached.source == "api"
            and _is_same_utc_day(cached, now)
            and not self._needs_status_confirmation(cached)
        ):
            if cached.remaining <= 0:
                self._remember_status(cached)
                return cached
            if self._status_confirms_buffer():
                return self._status_quota or cached
            fetched = cached.fetched_at
            if fetched is not None:
                age = (now - _as_utc(fetched)).total_seconds()
                if age < self._status_refresh_seconds:
                    self._remember_status(cached)
                    return cached
        return await self._fetch_status(now)

    async def _fetch_status(self, now: datetime) -> QuotaSnapshot:
        cached = self._quota
        try:
            snapshot = await self._client.get_status()
        except AuthBlockedError:
            raise
        except QuotaExhaustedError:
            observed = self._client_quota()
            if observed is not None and observed.remaining <= 0:
                self._remember_status(observed)
                return observed
            spent = QuotaSnapshot(
                current=self._settings.quota_daily_limit,
                limit_day=self._settings.quota_daily_limit,
                remaining=0,
                fetched_at=now,
                source="api",
            )
            self._remember_status(spent)
            return spent
        except RetryableHttpError, FootballHttpError:
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
        self._remember_status(snapshot)
        return snapshot

    async def _persist_cursors(self) -> None:
        async with self._session_factory() as session, session.begin():
            await ensure_cursors(session)

    def _sleep_seconds(self) -> float:
        now = self._now()
        until_reset = seconds_until_utc_midnight(now)
        if self._probing_reset():
            return min(self._idle_cap_seconds, until_reset)
        if self._status_confirms_buffer():
            return until_reset
        quota = self._quota
        if quota is not None and quota.source == "api" and quota.remaining <= 0:
            return min(self._idle_cap_seconds, until_reset)
        if quota is not None and self._inside_buffer(quota):
            return min(self._idle_cap_seconds, until_reset)
        if self._live_interval_seconds is not None:
            return self._live_interval_seconds
        return float(self._settings.live_poll_target_seconds)

    def _remember_status(self, snapshot: QuotaSnapshot) -> None:
        if snapshot.source == "api" and snapshot.reading == "status":
            self._status_quota = snapshot

    def _client_calls(self) -> int:
        made = getattr(self._client, "calls_made", None)
        if isinstance(made, int):
            return made
        return 0

    def _calls_this_tick(self) -> int:
        return max(0, self._client_calls() - self._calls_baseline)

    def _buffer_of(self, snapshot: QuotaSnapshot) -> int:
        limit = snapshot.limit_day or self._settings.quota_daily_limit
        return safety_buffer_requests(limit, self._settings.quota_safety_buffer_percent)

    def _inside_buffer(self, snapshot: QuotaSnapshot) -> bool:
        return snapshot.remaining <= self._buffer_of(snapshot)

    def _current_near_limit(self, snapshot: QuotaSnapshot) -> bool:
        limit = snapshot.limit_day or self._settings.quota_daily_limit
        if limit <= 0:
            return False
        return snapshot.current >= limit - self._buffer_of(snapshot)

    def _status_is_spent_today(self) -> bool:
        status = self._status_quota
        if status is None or status.reading != "status" or status.source != "api":
            return False
        if not _is_same_utc_day(status, self._now()):
            return False
        return self._inside_buffer(status)

    def _probing_reset(self) -> bool:
        status = self._status_quota
        if status is None or not self._status_is_spent_today():
            return False
        if not self._current_near_limit(status):
            return False
        return _seconds_since_utc_midnight(self._now()) < RESET_PROBE_SECONDS

    def _status_confirms_buffer(self) -> bool:
        return self._status_is_spent_today() and not self._probing_reset()

    def _needs_status_confirmation(self, snapshot: QuotaSnapshot) -> bool:
        """Headers inside the buffer still need a ``/status`` from today."""
        if not self._inside_buffer(snapshot):
            return False
        return not self._status_confirms_buffer()
