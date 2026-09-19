"""W7 pre-match: H2H, predictions, /odds, /odds/mapping. Runtime priority 8."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, NamedTuple

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.client.errors import (
    AuthBlockedError,
    FootballHttpError,
    QuotaExhaustedError,
    RetryableHttpError,
)
from predictor.client.football import FootballClient
from predictor.constants import PREMATCH_PER_TICK, URGENT_PREMATCH_PER_TICK
from predictor.logutil import log_json
from predictor.models.catalog import LeagueSeason
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.schemas.odds import OddsMappingItem, PrematchOddsItem
from predictor.schemas.predictions import PredictionItem
from predictor.services.ingest.drift import warn_model_extra
from predictor.services.ingest.fixtures import parse_fixtures
from predictor.services.ingest.paging import fetch_all_pages
from predictor.services.ingest.persist_fixtures import upsert_fixtures
from predictor.services.ingest.persist_odds import (
    persist_prematch_odds,
    replace_odds_mapping,
)
from predictor.services.ingest.persist_predictions import persist_predictions
from predictor.services.queue import (
    claim_h2h_task,
    claim_odds_task,
    claim_predictions_task,
    claim_urgent_odds_task,
    claim_urgent_predictions_task,
    complete_task,
    ensure_odds_task,
    get_or_create_endpoint_task,
    mapping_needs_refresh,
)


class _FixtureRef(NamedTuple):
    id: int
    league_id: int
    season: int


class PrematchIngest:
    def __init__(
        self,
        client: FootballClient,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now_fn: Callable[[], datetime] | None = None,
        per_tick: int = PREMATCH_PER_TICK,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._per_tick = per_tick

    def _quota_left(self) -> bool:
        snapshot = self._client.quota
        if snapshot is None:
            return True
        return snapshot.remaining > 0

    async def refresh_pending(self) -> None:
        if not self._quota_left():
            return
        await self._refresh_mapping()
        for _ in range(self._per_tick):
            if not self._quota_left():
                return
            if await self._one_h2h():
                continue
            if await self._one_predictions():
                continue
            if await self._one_odds():
                continue
            break

    async def refresh_urgent(self) -> None:
        """Drain odds then predictions for fixtures within the kickoff horizon (P3)."""
        if not self._quota_left():
            return
        for _ in range(URGENT_PREMATCH_PER_TICK):
            if not self._quota_left():
                return
            if await self._one_odds(urgent=True):
                continue
            if await self._one_predictions(urgent=True):
                continue
            break

    async def _refresh_mapping(self) -> None:
        if not self._quota_left():
            return
        task_id = await self._mapping_task_id()
        if task_id is None:
            return
        try:
            items, current, total = await fetch_all_pages(self._client, "/odds/mapping")
        except AuthBlockedError:
            raise
        except QuotaExhaustedError:
            await self._fail(task_id, "retryable_error", "quota_exhausted")
            return
        except (RetryableHttpError, FootballHttpError) as exc:
            await self._fail(task_id, "retryable_error", type(exc).__name__)
            return
        parsed = _parse_mapping(items)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    task = await session.get(EtlTask, task_id)
                    if task is None:
                        return
                    extra = await replace_odds_mapping(session, parsed, now=self._now())
                    for fixture_id in extra.get("fixture_ids", []):
                        await ensure_odds_task(session, int(fixture_id))
                    params = dict(task.params)
                    params.update(
                        {
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
                event="odds_mapping_persist_failed",
                endpoint="/odds/mapping",
            )
            await self._fail(task_id, "retryable_error", "persist_failed")

    async def _one_h2h(self) -> bool:
        claimed = await self._claim_h2h()
        if claimed is None:
            return False
        task_id, h2h = claimed
        try:
            items, current, total = await fetch_all_pages(
                self._client, "/fixtures/headtohead", params={"h2h": h2h}
            )
        except AuthBlockedError:
            raise
        except QuotaExhaustedError:
            await self._fail(task_id, "retryable_error", "quota_exhausted")
            return True
        except (RetryableHttpError, FootballHttpError) as exc:
            await self._fail(task_id, "retryable_error", type(exc).__name__)
            return True
        parsed = parse_fixtures(items)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    task = await session.get(EtlTask, task_id)
                    if task is None:
                        return True
                    extra = await upsert_fixtures(session, parsed)
                    params = dict(task.params)
                    params.update(
                        {
                            "h2h": h2h,
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
                event="h2h_persist_failed",
                endpoint="/fixtures/headtohead",
                error="persist_failed",
            )
            await self._fail(task_id, "retryable_error", "persist_failed")
        return True

    async def _one_predictions(self, *, urgent: bool = False) -> bool:
        task_id = await self._claim_predictions_id(urgent=urgent)
        if task_id is None:
            return False
        loaded = await self._load_task_fixture(task_id)
        if loaded is None:
            await self._fail(task_id, "permanent_error", "missing_fixture")
            return True
        if not await self._coverage_allowed(
            loaded.league_id, loaded.season, "predictions"
        ):
            await self._finish(
                task_id,
                "coverage_empty",
                params={"fixture": loaded.id, "reason": "coverage_false"},
            )
            return True
        try:
            items, current, total = await fetch_all_pages(
                self._client, "/predictions", params={"fixture": loaded.id}
            )
        except AuthBlockedError:
            raise
        except QuotaExhaustedError:
            await self._fail(task_id, "retryable_error", "quota_exhausted")
            return True
        except (RetryableHttpError, FootballHttpError) as exc:
            await self._fail(task_id, "retryable_error", type(exc).__name__)
            return True
        if not items:
            await self._finish(
                task_id,
                "coverage_empty",
                params={"fixture": loaded.id},
                paging_current=current,
                paging_total=total,
            )
            return True
        parsed = _parse_predictions(items)
        if not parsed:
            await self._fail(task_id, "retryable_error", "invalid_prediction")
            return True
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    task = await session.get(EtlTask, task_id)
                    if task is None:
                        return True
                    extra = await persist_predictions(
                        session, loaded.id, parsed, now=self._now()
                    )
                    params = dict(task.params)
                    params.update(
                        {
                            "fixture": loaded.id,
                            "h2h": extra.get("h2h", 0),
                            "links": extra.get("links", 0),
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
        except Exception as exc:
            log_json(
                logging.ERROR,
                service="worker",
                event="predictions_persist_failed",
                endpoint="/predictions",
                error=type(exc).__name__,
            )
            await self._fail(task_id, "retryable_error", type(exc).__name__)
        return True

    async def _one_odds(self, *, urgent: bool = False) -> bool:
        task_id = await self._claim_odds_id(urgent=urgent)
        if task_id is None:
            return False
        loaded = await self._load_task_fixture(task_id)
        if loaded is None:
            await self._fail(task_id, "permanent_error", "missing_fixture")
            return True
        if not await self._coverage_allowed(loaded.league_id, loaded.season, "odds"):
            await self._finish(
                task_id,
                "coverage_empty",
                params={"fixture": loaded.id, "reason": "coverage_false"},
            )
            return True
        try:
            items, current, total = await fetch_all_pages(
                self._client, "/odds", params={"fixture": loaded.id}
            )
        except AuthBlockedError:
            raise
        except QuotaExhaustedError:
            await self._fail(task_id, "retryable_error", "quota_exhausted")
            return True
        except (RetryableHttpError, FootballHttpError) as exc:
            await self._fail(task_id, "retryable_error", type(exc).__name__)
            return True
        if not items:
            await self._finish(
                task_id,
                "coverage_empty",
                params={"fixture": loaded.id},
                paging_current=current,
                paging_total=total,
            )
            return True
        parsed = _parse_odds(items)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    task = await session.get(EtlTask, task_id)
                    if task is None:
                        return True
                    extra = await persist_prematch_odds(session, parsed)
                    params = dict(task.params)
                    params.update(
                        {
                            "fixture": loaded.id,
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
        except Exception as exc:
            log_json(
                logging.ERROR,
                service="worker",
                event="odds_persist_failed",
                endpoint="/odds",
                error=type(exc).__name__,
            )
            await self._fail(task_id, "retryable_error", type(exc).__name__)
        return True

    async def _mapping_task_id(self) -> int | None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await get_or_create_endpoint_task(session, "/odds/mapping")
                if not mapping_needs_refresh(task, self._now()):
                    return None
                return int(task.id)

    async def _claim_h2h(self) -> tuple[int, str] | None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await claim_h2h_task(session)
                if task is None:
                    return None
                raw = task.params.get("h2h") if task.params else None
                if not isinstance(raw, str) or not raw.strip():
                    await complete_task(
                        session, task, "permanent_error", error="bad_h2h_params"
                    )
                    return None
                return int(task.id), raw.strip()

    async def _claim_predictions_id(self, *, urgent: bool = False) -> int | None:
        async with self._session_factory() as session:
            async with session.begin():
                if urgent:
                    task = await claim_urgent_predictions_task(session, now=self._now())
                else:
                    task = await claim_predictions_task(session)
                return None if task is None else int(task.id)

    async def _claim_odds_id(self, *, urgent: bool = False) -> int | None:
        async with self._session_factory() as session:
            async with session.begin():
                if urgent:
                    task = await claim_urgent_odds_task(session, now=self._now())
                else:
                    task = await claim_odds_task(session)
                return None if task is None else int(task.id)

    async def _load_task_fixture(self, task_id: int) -> _FixtureRef | None:
        async with self._session_factory() as session:
            task = await session.get(EtlTask, task_id)
            if task is None or task.fixture_id is None:
                return None
            fixture = await session.get(Fixture, task.fixture_id)
            if fixture is None:
                return None
            return _FixtureRef(
                id=int(fixture.id),
                league_id=int(fixture.league_id),
                season=int(fixture.season),
            )

    async def _coverage_allowed(self, league_id: int, season: int, kind: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(LeagueSeason, (league_id, season))
        if row is None:
            return True
        flag = row.cov_predictions if kind == "predictions" else row.cov_odds
        return flag is not False

    async def _finish(
        self,
        task_id: int,
        status: str,
        *,
        params: dict[str, Any] | None = None,
        paging_current: int | None = None,
        paging_total: int | None = None,
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await session.get(EtlTask, task_id)
                if task is None:
                    return
                merged = dict(task.params)
                if params:
                    merged.update(params)
                await complete_task(
                    session,
                    task,
                    status,
                    params=merged,
                    paging_current=paging_current,
                    paging_total=paging_total,
                )

    async def _fail(self, task_id: int, status: str, error: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await session.get(EtlTask, task_id)
                if task is None:
                    return
                await complete_task(session, task, status, error=error)


def _parse_predictions(raw: list[Any]) -> list[PredictionItem]:
    items: list[PredictionItem] = []
    for row in raw:
        try:
            item = PredictionItem.model_validate(row)
        except ValidationError:
            log_json(
                logging.WARNING,
                service="worker",
                event="catalog_row_invalid",
                endpoint="/predictions",
            )
            continue
        warn_model_extra("/predictions", item)
        if item.predictions is not None:
            warn_model_extra("/predictions", item.predictions)
        items.append(item)
    return items


def _parse_odds(raw: list[Any]) -> list[PrematchOddsItem]:
    items: list[PrematchOddsItem] = []
    for row in raw:
        try:
            item = PrematchOddsItem.model_validate(row)
        except ValidationError:
            log_json(
                logging.WARNING,
                service="worker",
                event="catalog_row_invalid",
                endpoint="/odds",
            )
            continue
        warn_model_extra("/odds", item)
        for bookmaker in item.bookmakers:
            warn_model_extra("/odds", bookmaker)
            for bet in bookmaker.bets:
                warn_model_extra("/odds", bet)
        items.append(item)
    return items


def _parse_mapping(raw: list[Any]) -> list[OddsMappingItem]:
    items: list[OddsMappingItem] = []
    for row in raw:
        try:
            item = OddsMappingItem.model_validate(row)
        except ValidationError:
            log_json(
                logging.WARNING,
                service="worker",
                event="catalog_row_invalid",
                endpoint="/odds/mapping",
            )
            continue
        warn_model_extra("/odds/mapping", item)
        items.append(item)
    return items
