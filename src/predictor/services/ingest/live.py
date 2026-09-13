"""W5 live path: /fixtures?live=all then append-only /odds/live (FR-015…FR-020)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.client.errors import (
    AuthBlockedError,
    FootballHttpError,
    QuotaExhaustedError,
    RetryableHttpError,
)
from predictor.client.football import FootballClient
from predictor.logutil import log_json
from predictor.models.etl import EtlTask
from predictor.schemas.live import OddsLiveItem
from predictor.services.ingest.drift import warn_model_extra
from predictor.services.ingest.fixtures import parse_fixtures
from predictor.services.ingest.paging import fetch_all_pages
from predictor.services.ingest.persist_fixtures import upsert_fixtures
from predictor.services.ingest.persist_live import (
    next_goal_ids_from_mapping,
    persist_odds_live_snapshots,
)
from predictor.services.queue import (
    complete_task,
    ensure_enrichment_task,
    ensure_injuries_task,
    ensure_rounds_task,
    get_or_create_endpoint_task,
    get_or_create_live_fixtures_task,
)


class LiveIngest:
    def __init__(
        self,
        client: FootballClient,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        target_seconds: int = 60,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._target_seconds = target_seconds
        self._now = now_fn or (lambda: datetime.now(UTC))
        self.last_live_fixture_ids: list[int] = []

    def _quota_left(self) -> bool:
        snapshot = self._client.quota
        if snapshot is None:
            return True
        return snapshot.remaining > 0

    async def refresh_live_fixtures(self) -> None:
        if not self._quota_left():
            return
        task_id = await self._live_fixtures_task_id()
        if task_id is None:
            return
        try:
            items, current, total = await fetch_all_pages(
                self._client, "/fixtures", params={"live": "all"}
            )
        except AuthBlockedError:
            raise
        except QuotaExhaustedError:
            await self._fail(task_id, "retryable_error", "quota_exhausted")
            return
        except (RetryableHttpError, FootballHttpError) as exc:
            await self._fail(task_id, "retryable_error", type(exc).__name__)
            return
        parsed = parse_fixtures(items)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    task = await session.get(EtlTask, task_id)
                    if task is None:
                        return
                    extra = await upsert_fixtures(session, parsed)
                    fixture_ids = [int(fid) for fid in extra.get("fixture_ids", [])]
                    for fixture_id in fixture_ids:
                        await ensure_enrichment_task(session, fixture_id)
                    for pair in extra.get("league_seasons", []):
                        await ensure_rounds_task(
                            session, int(pair["league"]), int(pair["season"])
                        )
                        await ensure_injuries_task(
                            session, int(pair["league"]), int(pair["season"])
                        )
                    params = dict(task.params)
                    params.update(
                        {
                            "live": "all",
                            "count": extra.get("count", 0),
                            "skipped": extra.get("skipped", 0),
                            "fixture_ids": fixture_ids,
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
                    self.last_live_fixture_ids = fixture_ids
        except Exception:
            log_json(
                logging.ERROR,
                service="worker",
                event="live_fixtures_persist_failed",
                endpoint="/fixtures",
            )
            await self._fail(task_id, "retryable_error", "persist_failed")

    async def refresh_next_goal_snapshots(self) -> None:
        if not self._quota_left():
            return
        task_id = await self._odds_live_task_id()
        if task_id is None:
            return
        mapped = await self._mapped_next_goal_ids()
        previous_poll = await self._last_poll_at(task_id)
        now = self._now()
        if previous_poll is not None:
            gap = (now - previous_poll).total_seconds()
            if gap > self._target_seconds:
                log_json(
                    logging.WARNING,
                    service="worker",
                    event="live_gap",
                    gap_seconds=round(gap, 3),
                    target_seconds=self._target_seconds,
                    last_poll_at=previous_poll.isoformat(),
                )
        try:
            items, current, total = await fetch_all_pages(self._client, "/odds/live")
        except AuthBlockedError:
            raise
        except QuotaExhaustedError:
            await self._fail(task_id, "retryable_error", "quota_exhausted")
            return
        except (RetryableHttpError, FootballHttpError) as exc:
            await self._fail(task_id, "retryable_error", type(exc).__name__)
            return
        parsed = _parse_odds_live(items)
        captured_at = self._now()
        extra: dict[str, Any] = {}
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    task = await session.get(EtlTask, task_id)
                    if task is None:
                        return
                    extra = await persist_odds_live_snapshots(
                        session,
                        parsed,
                        captured_at=captured_at,
                        mapped_next_goal=mapped,
                        discovered_fixture_ids=set(self.last_live_fixture_ids),
                    )
                    for fixture_id in extra.get("stored_fixture_ids", []):
                        await ensure_enrichment_task(session, int(fixture_id))
                    params = dict(task.params)
                    params.update(
                        {
                            "count": extra.get("count", 0),
                            "fixtures": extra.get("fixtures", 0),
                            "missing_next_goal": extra.get("missing_next_goal", []),
                            "blocked_without_odds": extra.get(
                                "blocked_without_odds", []
                            ),
                            "last_poll_at": captured_at.isoformat(),
                            "mapped_next_goal": sorted(mapped),
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
            missing = extra.get("missing_next_goal", [])
            if missing:
                log_json(
                    logging.INFO,
                    service="worker",
                    event="next_goal_absent",
                    count=len(missing),
                )
            blocked_empty = extra.get("blocked_without_odds", [])
            if blocked_empty:
                log_json(
                    logging.INFO,
                    service="worker",
                    event="next_goal_blocked",
                    count=len(blocked_empty),
                )
        except Exception:
            log_json(
                logging.ERROR,
                service="worker",
                event="odds_live_persist_failed",
                endpoint="/odds/live",
            )
            await self._fail(task_id, "retryable_error", "persist_failed")

    async def _live_fixtures_task_id(self) -> int | None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await get_or_create_live_fixtures_task(session)
                return int(task.id)

    async def _odds_live_task_id(self) -> int | None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await get_or_create_endpoint_task(session, "/odds/live")
                return int(task.id)

    async def _mapped_next_goal_ids(self) -> set[int]:
        async with self._session_factory() as session:
            task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/odds/live/bets")
                .where(EtlTask.cursor_kind.is_(None))
                .order_by(EtlTask.id)
                .limit(1)
            )
        if task is None:
            return set()
        return next_goal_ids_from_mapping(task.params.get("next_goal_bet_ids"))

    async def _last_poll_at(self, task_id: int) -> datetime | None:
        async with self._session_factory() as session:
            task = await session.get(EtlTask, task_id)
        if task is None:
            return None
        raw = task.params.get("last_poll_at") if task.params else None
        if not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    async def _fail(self, task_id: int, status: str, error: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await session.get(EtlTask, task_id)
                if task is None:
                    return
                await complete_task(session, task, status, error=error)


def _parse_odds_live(raw: list[Any]) -> list[OddsLiveItem]:
    items: list[OddsLiveItem] = []
    for row in raw:
        try:
            item = OddsLiveItem.model_validate(row)
        except ValidationError:
            log_json(
                logging.WARNING,
                service="worker",
                event="catalog_row_invalid",
                endpoint="/odds/live",
            )
            continue
        warn_model_extra("/odds/live", item)
        if item.fixture.status is not None:
            warn_model_extra("/odds/live", item.fixture.status)
        for bet in item.odds:
            warn_model_extra("/odds/live", bet)
            for value in bet.values:
                warn_model_extra("/odds/live", value)
        items.append(item)
    return items
