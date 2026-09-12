"""Runtime scheduler (FR-023). W2: quota loop and empty domain slots."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.client.errors import (
    AuthBlockedError,
    FootballHttpError,
    RetryableHttpError,
)
from predictor.client.football import FootballClient
from predictor.client.quota import QuotaSnapshot
from predictor.logutil import log_json
from predictor.schemas.settings import Settings
from predictor.services.queue import ensure_cursors
from predictor.services.quota import quota_allows, seconds_until_utc_midnight

Handler = Callable[[], Awaitable[None]]


class PrioritySlot(NamedTuple):
    priority: int
    name: str


PRIORITY_ORDER: tuple[PrioritySlot, ...] = (
    PrioritySlot(1, "quota_and_live_dictionaries"),
    PrioritySlot(2, "live_fixtures"),
    PrioritySlot(3, "next_goal_snapshots"),
    PrioritySlot(4, "finalization"),
    PrioritySlot(5, "forward_sync"),
    PrioritySlot(6, "enrichment"),
    PrioritySlot(7, "backfill"),
    PrioritySlot(8, "global_entities"),
)

STATUS_REFRESH_SECONDS = 3600.0


async def _noop() -> None:
    return None


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
    ) -> None:
        self._settings = settings
        self._client = client
        self._session_factory = session_factory
        self._handlers = dict(handlers or {})
        self._idle_cap_seconds = idle_cap_seconds
        self._status_refresh_seconds = status_refresh_seconds
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._quota: QuotaSnapshot | None = None

    @property
    def quota(self) -> QuotaSnapshot | None:
        return self._quota

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
        for slot in PRIORITY_ORDER:
            if stop.is_set():
                return
            if not quota_allows(
                slot.priority, quota, self._settings.quota_safety_buffer_percent
            ):
                continue
            handler = self._handlers.get(slot.priority, _noop)
            await handler()

    async def _ensure_quota(self) -> QuotaSnapshot:
        now = self._now()
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
        return float(self._settings.live_poll_target_seconds)
