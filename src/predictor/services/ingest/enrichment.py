"""W6 enrichment: /fixtures?id=, half statistics, injuries, FT control refresh."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
from predictor.constants import (
    CONTROL_REFRESH_HOURS,
    ENRICHMENT_PER_TICK,
    HALF_STATS_FROM_SEASON,
    IRREGULAR_FIXTURE_STATUSES,
)
from predictor.logutil import log_json
from predictor.models.catalog import LeagueSeason
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.schemas.enrichment import (
    FixtureDetail,
    InjuryItem,
    TeamStatisticsItem,
)
from predictor.services.ingest.drift import warn_model_extra
from predictor.services.ingest.paging import fetch_all_pages
from predictor.services.ingest.persist_children import (
    persist_fixture_detail,
    persist_injuries,
    upsert_team_statistics,
)
from predictor.services.queue import (
    claim_control_refresh_task,
    claim_enrichment_task,
    claim_half_stats_task,
    claim_injuries_task,
    complete_task,
    ensure_half_stats_task,
    get_cursor_task,
)


class _FixtureRef(NamedTuple):
    id: int
    status_short: str | None
    league_id: int
    season: int


class EnrichmentIngest:
    def __init__(
        self,
        client: FootballClient,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now_fn: Callable[[], datetime] | None = None,
        per_tick: int = ENRICHMENT_PER_TICK,
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

    async def refresh_finalization(self) -> None:
        for _ in range(self._per_tick):
            if not self._quota_left():
                return
            task_id = await self._claim_control_id()
            if task_id is None:
                return
            await self._enrich_task(task_id, control=True)

    async def refresh_pending(self) -> None:
        for _ in range(self._per_tick):
            if not self._quota_left():
                return
            task_id = await self._claim_enrichment_id()
            if task_id is None:
                break
            await self._enrich_task(task_id, control=False)
        await self._drain_half_stats()
        await self._drain_injuries()

    async def _claim_enrichment_id(self) -> int | None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await claim_enrichment_task(session)
                return None if task is None else int(task.id)

    async def _claim_control_id(self) -> int | None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await claim_control_refresh_task(session, self._now())
                return None if task is None else int(task.id)

    async def _enrich_task(self, task_id: int, *, control: bool) -> None:
        loaded = await self._load_task_fixture(task_id)
        if loaded is None:
            await self._fail(task_id, "permanent_error", "missing_fixture")
            return
        fixture = loaded
        if fixture.status_short in IRREGULAR_FIXTURE_STATUSES:
            await self._finish(
                task_id,
                "coverage_empty",
                params={
                    "id": fixture.id,
                    "reason": "irregular_status",
                    "status_short": fixture.status_short,
                },
            )
            return
        try:
            items, current, total = await fetch_all_pages(
                self._client, "/fixtures", params={"id": fixture.id}
            )
        except AuthBlockedError:
            raise
        except QuotaExhaustedError:
            await self._fail(task_id, "retryable_error", "quota_exhausted")
            return
        except (RetryableHttpError, FootballHttpError) as exc:
            await self._fail(task_id, "retryable_error", type(exc).__name__)
            return
        if not items:
            await self._finish(task_id, "coverage_empty", params={"id": fixture.id})
            return
        try:
            detail = FixtureDetail.model_validate(items[0])
        except ValidationError:
            log_json(
                logging.WARNING,
                service="worker",
                event="catalog_row_invalid",
                endpoint="/fixtures",
            )
            await self._fail(task_id, "retryable_error", "invalid_fixture_detail")
            return
        warn_model_extra("/fixtures", detail)
        coverage = await self._coverage(fixture.league_id, fixture.season)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    task = await session.get(EtlTask, task_id)
                    if task is None:
                        return
                    counts = await persist_fixture_detail(
                        session,
                        detail,
                        include_events=_allowed(coverage["events"]),
                        include_lineups=_allowed(coverage["lineups"]),
                        include_statistics=_allowed(coverage["statistics"]),
                        include_players=_allowed(coverage["players"]),
                    )
                    if (
                        _allowed(coverage["statistics"])
                        and fixture.season >= HALF_STATS_FROM_SEASON
                    ):
                        await ensure_half_stats_task(session, fixture.id)
                    now = self._now()
                    params = dict(task.params)
                    params.update(
                        {
                            "id": fixture.id,
                            "counts": counts,
                            "control_due": (
                                now + timedelta(hours=CONTROL_REFRESH_HOURS)
                            ).isoformat(),
                            "control_done": control,
                        }
                    )
                    if control:
                        params["control_at"] = now.isoformat()
                    else:
                        params["ft_refresh_at"] = now.isoformat()
                    cursor = await get_cursor_task(session, "enrichment")
                    cursor_params = dict(cursor.params)
                    cursor_params["last_fixture_id"] = fixture.id
                    cursor.params = cursor_params
                    cursor.updated_at = now
                    status = (
                        "coverage_empty"
                        if not any(
                            _allowed(coverage[key])
                            for key in ("events", "lineups", "statistics", "players")
                        )
                        else "complete"
                    )
                    await complete_task(
                        session,
                        task,
                        status,
                        paging_current=current,
                        paging_total=total,
                        params=params,
                    )
        except Exception:
            log_json(
                logging.ERROR,
                service="worker",
                event="enrichment_persist_failed",
                endpoint="/fixtures",
            )
            await self._fail(task_id, "retryable_error", "persist_failed")

    async def _drain_half_stats(self) -> None:
        for _ in range(self._per_tick):
            if not self._quota_left():
                return
            task_id, fixture_id = await self._claim_half_id()
            if task_id is None or fixture_id is None:
                return
            try:
                items, current, total = await fetch_all_pages(
                    self._client,
                    "/fixtures/statistics",
                    params={"fixture": fixture_id, "half": "true"},
                )
            except AuthBlockedError:
                raise
            except QuotaExhaustedError:
                await self._fail(task_id, "retryable_error", "quota_exhausted")
                return
            except (RetryableHttpError, FootballHttpError) as exc:
                await self._fail(task_id, "retryable_error", type(exc).__name__)
                return
            parsed = _parse_team_stats(items)
            if not parsed:
                await self._finish(
                    task_id,
                    "coverage_empty",
                    params={"fixture": fixture_id, "half": "true", "count": 0},
                    paging_current=current,
                    paging_total=total,
                )
                continue
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        task = await session.get(EtlTask, task_id)
                        if task is None:
                            return
                        count = await upsert_team_statistics(
                            session,
                            fixture_id,
                            parsed,
                            default_period="FT",
                            include_default=False,
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
                    event="half_stats_persist_failed",
                    endpoint="/fixtures/statistics",
                )
                await self._fail(task_id, "retryable_error", "persist_failed")

    async def _drain_injuries(self) -> None:
        for _ in range(self._per_tick):
            if not self._quota_left():
                return
            claimed = await self._claim_injuries()
            if claimed is None:
                return
            task_id, league_id, season = claimed
            coverage = await self._coverage(league_id, season)
            if not _allowed(coverage["injuries"]):
                await self._finish(
                    task_id,
                    "coverage_empty",
                    params={
                        "league": league_id,
                        "season": season,
                        "reason": "coverage_false",
                    },
                )
                continue
            try:
                items, current, total = await fetch_all_pages(
                    self._client,
                    "/injuries",
                    params={"league": league_id, "season": season},
                )
            except AuthBlockedError:
                raise
            except QuotaExhaustedError:
                await self._fail(task_id, "retryable_error", "quota_exhausted")
                return
            except (RetryableHttpError, FootballHttpError) as exc:
                await self._fail(task_id, "retryable_error", type(exc).__name__)
                return
            parsed = _parse_injuries(items)
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        task = await session.get(EtlTask, task_id)
                        if task is None:
                            return
                        count = await persist_injuries(session, parsed)
                        params = dict(task.params)
                        params["count"] = count
                        status = "coverage_empty" if count == 0 else "complete"
                        await complete_task(
                            session,
                            task,
                            status,
                            paging_current=current,
                            paging_total=total,
                            params=params,
                        )
            except Exception:
                log_json(
                    logging.ERROR,
                    service="worker",
                    event="injuries_persist_failed",
                    endpoint="/injuries",
                )
                await self._fail(task_id, "retryable_error", "persist_failed")

    async def _claim_half_id(self) -> tuple[int | None, int | None]:
        async with self._session_factory() as session:
            async with session.begin():
                task = await claim_half_stats_task(session)
                if task is None:
                    return None, None
                fixture_id = task.fixture_id or task.params.get("fixture")
                try:
                    return (
                        int(task.id),
                        int(fixture_id) if fixture_id is not None else None,
                    )
                except (TypeError, ValueError):
                    await complete_task(
                        session, task, "permanent_error", error="bad_half_params"
                    )
                    return None, None

    async def _claim_injuries(self) -> tuple[int, int, int] | None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await claim_injuries_task(session)
                if task is None:
                    return None
                league_id = task.params.get("league")
                season = task.params.get("season")
                if league_id is None or season is None:
                    await complete_task(
                        session, task, "permanent_error", error="bad_injuries_params"
                    )
                    return None
                try:
                    return int(task.id), int(league_id), int(season)
                except (TypeError, ValueError):
                    await complete_task(
                        session, task, "permanent_error", error="bad_injuries_params"
                    )
                    return None

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
                status_short=fixture.status_short,
                league_id=int(fixture.league_id),
                season=int(fixture.season),
            )

    async def _coverage(self, league_id: int, season: int) -> dict[str, bool | None]:
        async with self._session_factory() as session:
            row = await session.get(LeagueSeason, (league_id, season))
        if row is None:
            return {
                "events": None,
                "lineups": None,
                "statistics": None,
                "players": None,
                "injuries": None,
            }
        return {
            "events": row.cov_events,
            "lineups": row.cov_lineups,
            "statistics": row.cov_statistics_fixtures,
            "players": row.cov_statistics_players,
            "injuries": row.cov_injuries,
        }

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


def _allowed(flag: bool | None) -> bool:
    return flag is not False


def _parse_team_stats(raw: list[Any]) -> list[TeamStatisticsItem]:
    items: list[TeamStatisticsItem] = []
    for row in raw:
        try:
            item = TeamStatisticsItem.model_validate(row)
        except ValidationError:
            log_json(
                logging.WARNING,
                service="worker",
                event="catalog_row_invalid",
                endpoint="/fixtures/statistics",
            )
            continue
        warn_model_extra("/fixtures/statistics", item)
        items.append(item)
    return items


def _parse_injuries(raw: list[Any]) -> list[InjuryItem]:
    items: list[InjuryItem] = []
    for row in raw:
        try:
            item = InjuryItem.model_validate(row)
        except ValidationError:
            log_json(
                logging.WARNING,
                service="worker",
                event="catalog_row_invalid",
                endpoint="/injuries",
            )
            continue
        warn_model_extra("/injuries", item)
        items.append(item)
    return items
