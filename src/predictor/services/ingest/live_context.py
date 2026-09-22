"""Drain the full context of fixtures currently on /fixtures?live=all.

Runs after the live score poll and before /odds/live. Historical pending
tasks outside this set are not claimed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.constants import (
    LIVE_CONTEXT_MAX_CALLS_PER_TICK,
    LIVE_CONTEXT_TIME_FRACTION,
    TOP_PLAYER_ENDPOINTS,
)
from predictor.logutil import log_json
from predictor.models.fixtures import Fixture
from predictor.services.ingest.enrichment import EnrichmentIngest
from predictor.services.ingest.fixtures import FixtureIngest
from predictor.services.ingest.global_entities import GlobalIngest
from predictor.services.ingest.prematch import PrematchIngest
from predictor.services.queue import (
    claim_live_fixture_task as claim_detail,
)
from predictor.services.queue import (
    ensure_live_followups,
    get_or_create_live_fixtures_task,
    h2h_param,
    reopen_live_detail_task,
    unfinished_context_fixture_ids,
)


def context_budget_left(
    *,
    calls: int,
    max_calls: int,
    elapsed: float,
    target: float,
    fraction: float,
    quota_left: bool,
) -> bool:
    if not quota_left or calls >= max_calls:
        return False
    return elapsed < target * fraction


@dataclass
class _Scope:
    fixture_ids: list[int] = field(default_factory=list)
    h2h_keys: list[str] = field(default_factory=list)
    needles: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    league_seasons: list[tuple[int, int]] = field(default_factory=list)


class LiveContextIngest:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        enrichment: EnrichmentIngest,
        prematch: PrematchIngest,
        global_ingest: GlobalIngest,
        fixtures: FixtureIngest,
        target_seconds: int = 60,
        max_calls: int = LIVE_CONTEXT_MAX_CALLS_PER_TICK,
        time_fraction: float = LIVE_CONTEXT_TIME_FRACTION,
        now_fn: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._enrichment = enrichment
        self._prematch = prematch
        self._global = global_ingest
        self._fixtures = fixtures
        self._target_seconds = float(target_seconds)
        self._max_calls = max_calls
        self._time_fraction = time_fraction
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._detail_cursor = 0

    def _quota_left(self) -> bool:
        snapshot = self._enrichment._client.quota
        if snapshot is None:
            return True
        return snapshot.remaining > 0

    async def refresh(
        self,
        live_ids: Sequence[int],
        *,
        finishing_ids: Sequence[int] | None = None,
    ) -> list[int]:
        live = _unique(live_ids)
        live_set = set(live)
        finishing = [fid for fid in _unique(finishing_ids or []) if fid not in live_set]
        if not live and not finishing:
            return []
        await self._enqueue([*finishing, *live])
        spend = _Spend(
            started=self._now(),
            max_calls=self._max_calls,
            target=self._target_seconds,
            fraction=self._time_fraction,
            now_fn=self._now,
            quota_fn=self._quota_left,
        )
        if finishing:
            await self._drain(finishing, spend)
        if live:
            await self._drain(live, spend)
        await self._refresh_details(live, spend)
        still = await self._prune_finishing(finishing)
        log_json(
            logging.INFO,
            service="worker",
            event="live_context_tick",
            calls=spend.calls,
            live=len(live),
            finishing=len(still),
        )
        return still

    async def _enqueue(self, fixture_ids: list[int]) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await ensure_live_followups(session, fixture_ids)

    async def _drain(self, fixture_ids: list[int], spend: _Spend) -> None:
        scope = await self._load_scope(fixture_ids)
        while spend.left():
            if await self._prematch._one_odds(
                fixture_ids=scope.fixture_ids, retry_empty=True
            ):
                spend.take()
                continue
            if await self._prematch._one_predictions(
                fixture_ids=scope.fixture_ids, retry_empty=True
            ):
                spend.take()
                continue
            if await self._prematch._one_h2h(h2h_keys=scope.h2h_keys, retry_empty=True):
                spend.take()
                continue
            if await self._global.ingest_one_scoped(scope.needles):
                spend.take()
                continue
            if await self._enrichment.ingest_one_scoped(scope.league_seasons):
                spend.take()
                continue
            if await self._fixtures.ingest_one_scoped_rounds(scope.league_seasons):
                spend.take()
                continue
            break

    async def _refresh_details(self, live_ids: list[int], spend: _Spend) -> None:
        if not live_ids:
            return
        start = self._detail_cursor % len(live_ids)
        ordered = live_ids[start:] + live_ids[:start]
        attempted = 0
        for fixture_id in ordered:
            if not spend.left():
                break
            attempted += 1
            if await self._refresh_detail(fixture_id):
                spend.take()
        self._detail_cursor = (start + attempted) % len(live_ids)

    async def _refresh_detail(self, fixture_id: int) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                await reopen_live_detail_task(session, fixture_id)
                task = await claim_detail(
                    session, "/fixtures", [fixture_id], now=self._now()
                )
                task_id = None if task is None else int(task.id)
        if task_id is None:
            return False
        await self._enrichment._enrich_task(task_id, control=False)
        return True

    async def _load_scope(self, fixture_ids: list[int]) -> _Scope:
        scope = _Scope(fixture_ids=list(fixture_ids))
        if not fixture_ids:
            return scope
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        Fixture.id,
                        Fixture.home_team_id,
                        Fixture.away_team_id,
                        Fixture.league_id,
                        Fixture.season,
                    ).where(Fixture.id.in_(fixture_ids))
                )
            ).all()
        h2h: set[str] = set()
        seasons: set[tuple[int, int]] = set()
        teams: set[int] = set()
        team_stats: set[tuple[int, int, int]] = set()
        for _fid, home_id, away_id, league_id, season in rows:
            home = int(home_id)
            away = int(away_id)
            league = int(league_id)
            year = int(season)
            h2h.add(h2h_param(home, away))
            seasons.add((league, year))
            teams.add(home)
            teams.add(away)
            team_stats.add((home, league, year))
            team_stats.add((away, league, year))
        scope.h2h_keys = sorted(h2h)
        scope.league_seasons = sorted(seasons)
        needles: list[tuple[str, dict[str, Any]]] = []
        for league, year in scope.league_seasons:
            league_params: dict[str, Any] = {"league": league, "season": year}
            needles.append(("/standings", league_params))
            needles.append(("/players", league_params))
            for endpoint in TOP_PLAYER_ENDPOINTS:
                needles.append((endpoint, dict(league_params)))
        for team_id in sorted(teams):
            needles.append(("/teams", {"id": team_id}))
            needles.append(("/players/squads", {"team": team_id}))
        for team_id, league, year in sorted(team_stats):
            needles.append(
                (
                    "/teams/statistics",
                    {"team": team_id, "league": league, "season": year},
                )
            )
        scope.needles = needles
        return scope

    async def _prune_finishing(self, finishing_ids: list[int]) -> list[int]:
        async with self._session_factory() as session:
            async with session.begin():
                still = await unfinished_context_fixture_ids(session, finishing_ids)
                task = await get_or_create_live_fixtures_task(session)
                params = dict(task.params or {})
                params["finishing_ids"] = still
                task.params = params
                task.updated_at = self._now()
                return still


class _Spend:
    def __init__(
        self,
        *,
        started: datetime,
        max_calls: int,
        target: float,
        fraction: float,
        now_fn: Any,
        quota_fn: Any,
    ) -> None:
        self.calls = 0
        self._started = started
        self._max_calls = max_calls
        self._target = target
        self._fraction = fraction
        self._now = now_fn
        self._quota = quota_fn

    def left(self) -> bool:
        elapsed = (self._now() - self._started).total_seconds()
        return context_budget_left(
            calls=self.calls,
            max_calls=self._max_calls,
            elapsed=elapsed,
            target=self._target,
            fraction=self._fraction,
            quota_left=bool(self._quota()),
        )

    def take(self) -> None:
        self.calls += 1


def _unique(raw: Sequence[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
