"""W3 dictionary ingest: paginated, idempotent, FK order, no /fixtures."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
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
from predictor.logutil import log_json
from predictor.models.catalog import Bookmaker, OddsBet, OddsLiveBet
from predictor.models.etl import EtlTask
from predictor.schemas.catalog import CountryItem, LeagueResponseItem, NamedIdItem
from predictor.services.ingest.drift import warn_model_extra
from predictor.services.ingest.next_goal import next_goal_matches
from predictor.services.ingest.paging import fetch_all_pages
from predictor.services.ingest.persist import (
    upsert_countries,
    upsert_leagues,
    upsert_named_ids,
    upsert_seasons,
)
from predictor.services.queue import (
    complete_task,
    get_or_create_endpoint_task,
    needs_refresh,
)

PersistFn = Callable[[AsyncSession, list[Any]], Awaitable[dict[str, Any]]]


class CatalogIngest:
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
        self.timezones: list[str] = []

    def _quota_left(self) -> bool:
        snapshot = self._client.quota
        if snapshot is None:
            return True
        return snapshot.remaining > 0

    async def refresh_priority_one(self) -> None:
        steps: tuple[tuple[str, PersistFn], ...] = (
            ("/timezone", self._persist_timezone),
            ("/countries", self._persist_countries),
            ("/teams/countries", self._persist_team_countries),
            ("/leagues/seasons", self._persist_seasons),
            ("/leagues", self._persist_leagues),
            ("/odds/bookmakers", self._persist_bookmakers),
            ("/odds/bets", self._persist_odds_bets),
            ("/odds/live/bets", self._persist_live_bets),
        )
        for endpoint, persist in steps:
            if not self._quota_left():
                return
            await self._ingest_endpoint(endpoint, persist)

    async def _ingest_endpoint(self, endpoint: str, persist: PersistFn) -> None:
        task_id = await self._task_id_if_due(endpoint)
        if task_id is None:
            return
        try:
            items, current, total = await fetch_all_pages(self._client, endpoint)
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
                    extra = await persist(session, items)
                    params = dict(task.params)
                    params.update(extra)
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
                event="catalog_persist_failed",
                endpoint=endpoint,
            )
            await self._fail(task_id, "retryable_error", "persist_failed")

    async def _task_id_if_due(self, endpoint: str) -> int | None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await get_or_create_endpoint_task(session, endpoint)
                if not needs_refresh(task, self._now()):
                    return None
                return int(task.id)

    async def _fail(self, task_id: int, status: str, error: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await session.get(EtlTask, task_id)
                if task is None:
                    return
                await complete_task(session, task, status, error=error)

    async def _persist_timezone(
        self, session: AsyncSession, items: list[Any]
    ) -> dict[str, Any]:
        del session
        self.timezones = [str(item) for item in items if item is not None]
        return {"count": len(self.timezones)}

    async def _persist_countries(
        self, session: AsyncSession, items: list[Any]
    ) -> dict[str, Any]:
        parsed = _parse_countries("/countries", items)
        count = await upsert_countries(session, parsed)
        return {"count": count}

    async def _persist_team_countries(
        self, session: AsyncSession, items: list[Any]
    ) -> dict[str, Any]:
        parsed = _parse_countries("/teams/countries", items)
        count = await upsert_countries(session, parsed)
        return {"count": count}

    async def _persist_seasons(
        self, session: AsyncSession, items: list[Any]
    ) -> dict[str, Any]:
        years = _parse_years(items)
        count = await upsert_seasons(session, years)
        return {"count": count}

    async def _persist_leagues(
        self, session: AsyncSession, items: list[Any]
    ) -> dict[str, Any]:
        parsed = _parse_leagues("/leagues", items)
        leagues, seasons = await upsert_leagues(session, parsed)
        return {"leagues": leagues, "league_seasons": seasons}

    async def _persist_bookmakers(
        self, session: AsyncSession, items: list[Any]
    ) -> dict[str, Any]:
        parsed = _parse_named("/odds/bookmakers", items)
        count = await upsert_named_ids(session, Bookmaker, parsed)
        return {"count": count}

    async def _persist_odds_bets(
        self, session: AsyncSession, items: list[Any]
    ) -> dict[str, Any]:
        parsed = _parse_named("/odds/bets", items)
        count = await upsert_named_ids(session, OddsBet, parsed)
        return {"count": count}

    async def _persist_live_bets(
        self, session: AsyncSession, items: list[Any]
    ) -> dict[str, Any]:
        parsed = _parse_named("/odds/live/bets", items)
        count = await upsert_named_ids(session, OddsLiveBet, parsed)
        matches = next_goal_matches([(item.id, item.name) for item in parsed])
        if not matches:
            log_json(
                logging.WARNING,
                service="worker",
                event="next_goal_unmapped",
                endpoint="/odds/live/bets",
            )
        return {
            "count": count,
            "next_goal_bet_ids": [bet_id for bet_id, _name in matches],
            "next_goal_names": [name for _bet_id, name in matches],
        }


def _parse_countries(endpoint: str, raw: Sequence[Any]) -> list[CountryItem]:
    items: list[CountryItem] = []
    for row in raw:
        try:
            item = CountryItem.model_validate(row)
        except ValidationError:
            log_json(
                logging.WARNING,
                service="worker",
                event="catalog_row_invalid",
                endpoint=endpoint,
            )
            continue
        warn_model_extra(endpoint, item)
        items.append(item)
    return items


def _parse_named(endpoint: str, raw: Sequence[Any]) -> list[NamedIdItem]:
    items: list[NamedIdItem] = []
    for row in raw:
        try:
            item = NamedIdItem.model_validate(row)
        except ValidationError:
            log_json(
                logging.WARNING,
                service="worker",
                event="catalog_row_invalid",
                endpoint=endpoint,
            )
            continue
        warn_model_extra(endpoint, item)
        items.append(item)
    return items


def _parse_leagues(endpoint: str, raw: Sequence[Any]) -> list[LeagueResponseItem]:
    items: list[LeagueResponseItem] = []
    for row in raw:
        try:
            item = LeagueResponseItem.model_validate(row)
        except ValidationError:
            log_json(
                logging.WARNING,
                service="worker",
                event="catalog_row_invalid",
                endpoint=endpoint,
            )
            continue
        warn_model_extra(endpoint, item)
        warn_model_extra(endpoint, item.league)
        if item.country is not None:
            warn_model_extra(endpoint, item.country)
        for season in item.seasons:
            warn_model_extra(endpoint, season)
            if season.coverage is not None:
                warn_model_extra(endpoint, season.coverage)
                if season.coverage.fixtures is not None:
                    warn_model_extra(endpoint, season.coverage.fixtures)
        items.append(item)
    return items


def _parse_years(raw: Sequence[Any]) -> list[int]:
    years: list[int] = []
    for row in raw:
        if isinstance(row, bool):
            continue
        try:
            years.append(int(row))
        except (TypeError, ValueError):
            log_json(
                logging.WARNING,
                service="worker",
                event="catalog_row_invalid",
                endpoint="/leagues/seasons",
            )
    return years
