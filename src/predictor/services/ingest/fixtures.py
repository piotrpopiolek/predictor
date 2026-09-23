"""W4 fixture ingest: /fixtures?date= forward/backfill and /fixtures/rounds."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.client.errors import (
    AuthBlockedError,
    FootballHttpError,
    QuotaExhaustedError,
    RetryableHttpError,
)
from predictor.client.football import FootballClient
from predictor.constants import HISTORICAL_BACKLOG_MAX_AGE_SECONDS
from predictor.logutil import log_json
from predictor.models.etl import EtlTask
from predictor.schemas.fixtures import FixtureItem
from predictor.services.completeness import day_is_complete
from predictor.services.ingest.drift import warn_model_extra
from predictor.services.ingest.paging import fetch_all_pages
from predictor.services.ingest.persist_fixtures import (
    upsert_fixtures,
    upsert_league_rounds,
)
from predictor.services.queue import (
    claim_live_param_task,
    claim_rounds_task,
    complete_task,
    enqueue_fixture_followups,
    get_cursor_task,
    get_or_create_day_task,
    historical_backlog_blocks,
    needs_refresh,
)

_ROUNDS_PER_TICK = 8


class FixtureIngest:
    def __init__(
        self,
        client: FootballClient,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._now = now_fn or (lambda: datetime.now(UTC))

    def _quota_left(self) -> bool:
        snapshot = self._client.quota
        if snapshot is None:
            return True
        return snapshot.remaining > 0

    def _today(self) -> date:
        return self._now().astimezone(UTC).date()

    async def refresh_forward(self) -> None:
        if not self._quota_left():
            return
        await self._ingest_day(self._today(), role="forward")
        await self.refresh_rounds_pending()

    async def refresh_backfill(self) -> None:
        if not self._quota_left():
            return
        if not await self._today_fixtures_complete():
            return
        if await self._historical_backlog_blocks():
            log_json(
                logging.INFO,
                service="worker",
                event="historical_enqueue_paused",
                max_age_seconds=HISTORICAL_BACKLOG_MAX_AGE_SECONDS,
            )
            return
        day = await self._peek_backfill_day()
        if day >= self._today():
            day = self._today() - timedelta(days=1)
        ok = await self._ingest_day(day, role="backfill")
        if not ok:
            return
        if await self._day_complete(day):
            await self._advance_backfill(day)

    async def _day_complete(self, day: date) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                return await day_is_complete(session, day)

    async def ingest_one_scoped_rounds(self, pairs: Sequence[tuple[int, int]]) -> bool:
        if not pairs or not self._quota_left():
            return False
        task_id, league_id, season = await self._claim_scoped_rounds(pairs)
        if task_id is None or league_id is None or season is None:
            return False
        await self._ingest_rounds(task_id, league_id, season)
        return True

    async def _claim_scoped_rounds(
        self, pairs: Sequence[tuple[int, int]]
    ) -> tuple[int | None, int | None, int | None]:
        async with self._session_factory() as session:
            async with session.begin():
                task = await claim_live_param_task(
                    session,
                    "/fixtures/rounds",
                    [
                        {"league": league_id, "season": season}
                        for league_id, season in pairs
                    ],
                    now=self._now(),
                )
                if task is None:
                    return None, None, None
                league_id = task.params.get("league")
                season = task.params.get("season")
                if league_id is None or season is None:
                    await complete_task(
                        session, task, "permanent_error", error="bad_rounds_params"
                    )
                    return None, None, None
                try:
                    return int(task.id), int(league_id), int(season)
                except (TypeError, ValueError):
                    await complete_task(
                        session, task, "permanent_error", error="bad_rounds_params"
                    )
                    return None, None, None

    async def refresh_rounds_pending(self) -> None:
        for _ in range(_ROUNDS_PER_TICK):
            if not self._quota_left():
                return
            task_id, league_id, season = await self._claim_rounds()
            if task_id is None or league_id is None or season is None:
                return
            await self._ingest_rounds(task_id, league_id, season)

    async def _historical_backlog_blocks(self) -> bool:
        async with self._session_factory() as session:
            return await historical_backlog_blocks(session, self._now())

    async def _today_fixtures_complete(self) -> bool:
        day = self._today()
        async with self._session_factory() as session:
            async with session.begin():
                task = await get_or_create_day_task(session, "/fixtures", day)
                return task.status == "complete"

    async def _peek_backfill_day(self) -> date:
        async with self._session_factory() as session:
            async with session.begin():
                cursor = await get_cursor_task(session, "backfill")
                raw = cursor.params.get("next_day") if cursor.params else None
        if isinstance(raw, str):
            try:
                return date.fromisoformat(raw)
            except ValueError:
                pass
        return self._today() - timedelta(days=1)

    async def _advance_backfill(self, completed: date) -> None:
        nxt = completed - timedelta(days=1)
        async with self._session_factory() as session:
            async with session.begin():
                cursor = await get_cursor_task(session, "backfill")
                params = dict(cursor.params)
                params["last_complete"] = completed.isoformat()
                params["next_day"] = nxt.isoformat()
                cursor.params = params
                cursor.day_utc = nxt
                cursor.updated_at = datetime.now(UTC)

    async def _touch_forward_cursor(self, day: date) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                cursor = await get_cursor_task(session, "forward")
                params = dict(cursor.params)
                params["day"] = day.isoformat()
                cursor.params = params
                cursor.day_utc = day
                cursor.updated_at = datetime.now(UTC)

    async def _ingest_day(self, day: date, *, role: str) -> bool:
        task_id = await self._day_task_id(day, refresh=role == "forward")
        if task_id is None:
            return True
        date_key = day.isoformat()
        try:
            items, current, total = await fetch_all_pages(
                self._client, "/fixtures", params={"date": date_key}
            )
        except AuthBlockedError:
            raise
        except QuotaExhaustedError:
            await self._fail(task_id, "retryable_error", "quota_exhausted")
            return False
        except (RetryableHttpError, FootballHttpError) as exc:
            await self._fail(task_id, "retryable_error", type(exc).__name__)
            return False
        parsed = parse_fixtures(items)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    task = await session.get(EtlTask, task_id)
                    if task is None:
                        return False
                    extra = await upsert_fixtures(session, parsed)
                    await enqueue_fixture_followups(
                        session, extra, historical=role == "backfill"
                    )
                    params = dict(task.params)
                    params.update(
                        {
                            "date": date_key,
                            "role": role,
                            "count": extra.get("count", 0),
                            "skipped": extra.get("skipped", 0),
                        }
                    )
                    await complete_task(
                        session,
                        task,
                        "complete",
                        paging_current=current,
                        paging_total=total,
                        params=params,
                    )
        except Exception:
            log_json(
                logging.ERROR,
                service="worker",
                event="fixtures_persist_failed",
                endpoint="/fixtures",
            )
            await self._fail(task_id, "retryable_error", "persist_failed")
            return False
        if role == "forward":
            await self._touch_forward_cursor(day)
        return True

    async def _day_task_id(self, day: date, *, refresh: bool) -> int | None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await get_or_create_day_task(session, "/fixtures", day)
                if refresh:
                    return int(task.id)
                if not needs_refresh(task, self._now()):
                    return None
                return int(task.id)

    async def _claim_rounds(self) -> tuple[int | None, int | None, int | None]:
        async with self._session_factory() as session:
            async with session.begin():
                task = await claim_rounds_task(session)
                if task is None:
                    return None, None, None
                league_id = task.params.get("league")
                season = task.params.get("season")
                if league_id is None or season is None:
                    await complete_task(
                        session, task, "permanent_error", error="bad_rounds_params"
                    )
                    return None, None, None
                try:
                    return int(task.id), int(league_id), int(season)
                except (TypeError, ValueError):
                    await complete_task(
                        session, task, "permanent_error", error="bad_rounds_params"
                    )
                    return None, None, None

    async def _ingest_rounds(self, task_id: int, league_id: int, season: int) -> None:
        try:
            items, current, total = await fetch_all_pages(
                self._client,
                "/fixtures/rounds",
                params={
                    "league": league_id,
                    "season": season,
                    "dates": "true",
                },
            )
        except AuthBlockedError:
            raise
        except QuotaExhaustedError:
            await self._fail(task_id, "retryable_error", "quota_exhausted")
            return
        except (RetryableHttpError, FootballHttpError) as exc:
            await self._fail(task_id, "retryable_error", type(exc).__name__)
            return
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    task = await session.get(EtlTask, task_id)
                    if task is None:
                        return
                    count = await upsert_league_rounds(
                        session, league_id, season, items
                    )
                    params = dict(task.params)
                    params["count"] = count
                    await complete_task(
                        session,
                        task,
                        "complete",
                        paging_current=current,
                        paging_total=total,
                        params=params,
                    )
        except Exception:
            log_json(
                logging.ERROR,
                service="worker",
                event="rounds_persist_failed",
                endpoint="/fixtures/rounds",
            )
            await self._fail(task_id, "retryable_error", "persist_failed")

    async def _fail(self, task_id: int, status: str, error: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await session.get(EtlTask, task_id)
                if task is None:
                    return
                await complete_task(session, task, status, error=error)


def parse_fixtures(raw: list[Any]) -> list[FixtureItem]:
    items: list[FixtureItem] = []
    for row in raw:
        try:
            item = FixtureItem.model_validate(row)
        except ValidationError:
            log_json(
                logging.WARNING,
                service="worker",
                event="catalog_row_invalid",
                endpoint="/fixtures",
            )
            continue
        warn_model_extra("/fixtures", item)
        warn_model_extra("/fixtures", item.fixture)
        warn_model_extra("/fixtures", item.league)
        warn_model_extra("/fixtures", item.teams)
        items.append(item)
    return items
