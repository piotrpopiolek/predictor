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
    FINISHED_FIXTURE_STATUSES,
    LIVE_CONTEXT_DETAIL_CALLS,
    LIVE_CONTEXT_FINAL_CALLS,
    LIVE_CONTEXT_MAX_CALLS_PER_TICK,
    LIVE_CONTEXT_ONESHOT_CALLS,
    LIVE_CONTEXT_TIME_FRACTION,
    LIVE_DETAIL_REFRESH_SECONDS,
    TOP_PLAYER_ENDPOINTS,
)
from predictor.logutil import log_json
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.services.ingest.enrichment import EnrichmentIngest
from predictor.services.ingest.fixtures import FixtureIngest
from predictor.services.ingest.global_entities import GlobalIngest
from predictor.services.ingest.prematch import PrematchIngest
from predictor.services.queue import (
    claim_live_fixture_task as claim_detail,
)
from predictor.services.queue import (
    ensure_enrichment_task,
    ensure_live_followups,
    get_or_create_live_fixtures_task,
    h2h_param,
    unfinished_context_fixture_ids,
)


def context_call_budget(
    *,
    detail_calls: int,
    oneshot_calls: int,
    hard_cap: int,
) -> int:
    """Ceiling for one tick: due match refreshes plus a short one-shot tail."""
    if hard_cap <= 0:
        return 0
    needed = max(0, detail_calls) + max(0, oneshot_calls)
    return min(hard_cap, needed)


@dataclass(frozen=True, slots=True)
class LiveContextResult:
    """Finishing matches still open, and vendor calls spent this tick."""

    finishing_ids: list[int]
    calls: int


def detail_refresh_due(
    *,
    task_status: str | None,
    params: dict[str, Any] | None,
    match_status: str | None,
    now: datetime,
    final: bool = False,
    refresh_seconds: int = LIVE_DETAIL_REFRESH_SECONDS,
) -> bool:
    """Events and match stats move during the game.

    A live match is fetched on sight, then every ``refresh_seconds``.
    A finished match gets one closing fetch. Lineups, odds and season
    tables stay on the one-shot path.
    """
    if task_status == "in_progress":
        return False
    if task_status in {None, "pending", "retryable_error"}:
        return True
    stored = params or {}
    closing = final or match_status in FINISHED_FIXTURE_STATUSES
    if closing:
        return not stored.get("final_detail_at")
    raw = stored.get("last_live_detail_at") or stored.get("ft_refresh_at")
    last = _parse_stamp(raw)
    if last is None:
        return True
    return (now - last).total_seconds() >= refresh_seconds


_LATE_FIRST_HALF_MINUTE = 40


def match_refresh_seconds(
    status: str | None,
    elapsed: int | None,
    *,
    urgent: int,
    relaxed: int,
) -> int:
    """Halftime and a late first half keep the faster detail cadence."""
    if status == "HT" or (
        status == "1H" and int(elapsed or 0) >= _LATE_FIRST_HALF_MINUTE
    ):
        return urgent
    return max(urgent, relaxed)


def detail_service_order(
    rows: Sequence[tuple[int, str | None, int | None, datetime | None]],
    *,
    now: datetime,
    refresh_seconds: int,
    cursor: int = 0,
) -> list[int]:
    """Prefer halftime and a late first half, then the stalest detail.

    The freshness bonus is one refresh interval, so a match left untouched
    for longer than that still moves ahead of a recently fetched favourite.
    Equal ages rotate with ``cursor`` so the same id is not always first.
    """
    bonus_window = float(max(1, refresh_seconds))
    indexed = list(enumerate(rows))
    count = len(indexed)

    def key(
        item: tuple[int, tuple[int, str | None, int | None, datetime | None]],
    ) -> tuple[float, int, int]:
        index, row = item
        fixture_id, status, elapsed, last = row
        if last is None:
            age = bonus_window * 4
        else:
            age = max(0.0, (now - last).total_seconds())
        bonus = 0.0
        if status == "HT":
            bonus = bonus_window
        elif status == "1H" and int(elapsed or 0) >= _LATE_FIRST_HALF_MINUTE:
            bonus = bonus_window / 2
        rotate = (index - cursor) % count if count else 0
        return (-(age + bonus), rotate, fixture_id)

    return [row[0] for _, row in sorted(indexed, key=key)]


def deadline_order(
    rows: Sequence[tuple[int, str | None, int | None]],
) -> list[int]:
    """Halftime first, then the latest minute of the first half, then the rest."""

    def key(row: tuple[int, str | None, int | None]) -> tuple[int, int, int]:
        fixture_id, status, elapsed = row
        minute = int(elapsed or 0)
        if status == "HT":
            return (0, 0, fixture_id)
        if status == "1H":
            return (1, -minute, fixture_id)
        return (2, -minute, fixture_id)

    return [fixture_id for fixture_id, _, _ in sorted(rows, key=key)]


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
        detail_refresh_seconds: int = LIVE_DETAIL_REFRESH_SECONDS,
        relaxed_refresh_seconds: int | None = None,
        oneshot_calls: int = LIVE_CONTEXT_ONESHOT_CALLS,
        max_context_calls: int | None = None,
    ) -> LiveContextResult:
        live = _unique(live_ids)
        live_set = set(live)
        finishing = [fid for fid in _unique(finishing_ids or []) if fid not in live_set]
        if not live and not finishing:
            return LiveContextResult(finishing_ids=[], calls=0)
        await self._enqueue([*finishing, *live])
        if max_context_calls is None:
            cap = context_call_budget(
                detail_calls=LIVE_CONTEXT_DETAIL_CALLS + LIVE_CONTEXT_FINAL_CALLS,
                oneshot_calls=oneshot_calls,
                hard_cap=self._max_calls,
            )
        else:
            cap = max(0, min(self._max_calls, max_context_calls))
        spend = _Spend(
            started=self._now(),
            max_calls=cap,
            target=self._target_seconds,
            fraction=self._time_fraction,
            now_fn=self._now,
            quota_fn=self._quota_left,
        )
        relaxed = (
            detail_refresh_seconds
            if relaxed_refresh_seconds is None
            else max(detail_refresh_seconds, relaxed_refresh_seconds)
        )
        await self._refresh_details(
            live,
            spend,
            final=False,
            limit=min(LIVE_CONTEXT_DETAIL_CALLS, cap),
            refresh_seconds=detail_refresh_seconds,
            relaxed_refresh_seconds=relaxed,
        )
        await self._refresh_details(
            finishing,
            spend,
            final=True,
            limit=min(LIVE_CONTEXT_FINAL_CALLS, max(0, cap - spend.calls)),
            refresh_seconds=detail_refresh_seconds,
            relaxed_refresh_seconds=relaxed,
        )
        urgent, later = await self._deadline_groups(live)
        oneshot_left = min(max(0, oneshot_calls), max(0, cap - spend.calls))
        for stage in ("odds", "predictions", "rest"):
            for group in (urgent, later, finishing):
                if group and spend.left() and oneshot_left:
                    oneshot_left = await self._drain(
                        group, spend, limit=oneshot_left, stage=stage
                    )
        still = await self._prune_finishing(finishing)
        log_json(
            logging.INFO,
            service="worker",
            event="live_context_tick",
            calls=spend.calls,
            live=len(live),
            finishing=len(still),
            cap=cap,
        )
        return LiveContextResult(finishing_ids=still, calls=spend.calls)

    async def _deadline_groups(
        self, fixture_ids: list[int]
    ) -> tuple[list[int], list[int]]:
        if not fixture_ids:
            return [], []
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        Fixture.id,
                        Fixture.status_short,
                        Fixture.elapsed_minutes,
                    ).where(Fixture.id.in_(fixture_ids))
                )
            ).all()
        ranked = deadline_order(
            [(int(fixture_id), status, elapsed) for fixture_id, status, elapsed in rows]
        )
        status_by_id = {int(fixture_id): status for fixture_id, status, _ in rows}
        urgent = [
            fixture_id
            for fixture_id in ranked
            if status_by_id.get(fixture_id) in {"HT", "1H"}
        ]
        later = [fixture_id for fixture_id in ranked if fixture_id not in set(urgent)]
        return urgent, later

    async def _enqueue(self, fixture_ids: list[int]) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await ensure_live_followups(session, fixture_ids)

    async def _drain(
        self,
        fixture_ids: list[int],
        spend: _Spend,
        *,
        limit: int,
        stage: str,
    ) -> int:
        scope = await self._load_scope(fixture_ids)
        used = 0
        while spend.left() and used < limit:
            if stage == "odds":
                if await self._prematch._one_odds(
                    fixture_ids=scope.fixture_ids, retry_empty=True
                ):
                    if self._prematch.used_http:
                        spend.take()
                        used += 1
                    continue
                break
            if stage == "predictions":
                if await self._prematch._one_predictions(
                    fixture_ids=scope.fixture_ids, retry_empty=True
                ):
                    if self._prematch.used_http:
                        spend.take()
                        used += 1
                    continue
                break
            if await self._prematch._one_h2h(h2h_keys=scope.h2h_keys, retry_empty=True):
                spend.take()
                used += 1
                continue
            if await self._global.ingest_one_scoped(scope.needles):
                spend.take()
                used += 1
                continue
            if await self._enrichment.ingest_one_scoped(scope.league_seasons):
                spend.take()
                used += 1
                continue
            if await self._fixtures.ingest_one_scoped_rounds(scope.league_seasons):
                spend.take()
                used += 1
                continue
            break
        return limit - used

    async def _refresh_details(
        self,
        live_ids: list[int],
        spend: _Spend,
        *,
        final: bool,
        limit: int,
        refresh_seconds: int = LIVE_DETAIL_REFRESH_SECONDS,
        relaxed_refresh_seconds: int | None = None,
    ) -> None:
        if not live_ids or limit <= 0 or not spend.left():
            return
        state = await self._detail_state(live_ids)
        ordered = detail_service_order(
            [_detail_row(fixture_id, state.get(fixture_id)) for fixture_id in live_ids],
            now=self._now(),
            refresh_seconds=refresh_seconds,
            cursor=0 if final else self._detail_cursor,
        )
        fetched = 0
        for fixture_id in ordered:
            if not spend.left() or fetched >= limit:
                break
            task_status, params, match_status, elapsed = state.get(
                fixture_id, (None, {}, None, None)
            )
            due_after = match_refresh_seconds(
                match_status,
                elapsed,
                urgent=refresh_seconds,
                relaxed=(
                    refresh_seconds
                    if relaxed_refresh_seconds is None
                    else relaxed_refresh_seconds
                ),
            )
            if not detail_refresh_due(
                task_status=task_status,
                params=params,
                match_status=match_status,
                now=self._now(),
                final=final,
                refresh_seconds=due_after,
            ):
                continue
            if await self._refresh_detail(
                fixture_id,
                final=final,
                match_status=match_status,
                refresh_seconds=due_after,
            ):
                spend.take()
                fetched += 1
        if not final:
            self._detail_cursor = (self._detail_cursor + max(1, fetched)) % len(
                live_ids
            )

    async def _detail_state(
        self, fixture_ids: list[int]
    ) -> dict[int, tuple[str | None, dict[str, Any], str | None, int | None]]:
        async with self._session_factory() as session:
            fixtures = (
                await session.execute(
                    select(
                        Fixture.id,
                        Fixture.status_short,
                        Fixture.elapsed_minutes,
                    ).where(Fixture.id.in_(fixture_ids))
                )
            ).all()
            tasks = (
                await session.execute(
                    select(EtlTask.fixture_id, EtlTask.status, EtlTask.params)
                    .where(EtlTask.endpoint == "/fixtures")
                    .where(EtlTask.fixture_id.in_(fixture_ids))
                    .where(EtlTask.cursor_kind.is_(None))
                )
            ).all()
        state: dict[int, tuple[str | None, dict[str, Any], str | None, int | None]] = {
            int(fixture_id): (
                None,
                {},
                None if status is None else str(status),
                None if elapsed is None else int(elapsed),
            )
            for fixture_id, status, elapsed in fixtures
        }
        for fixture_id, task_status, params in tasks:
            match_status, elapsed = state.get(int(fixture_id), (None, {}, None, None))[
                2:
            ]
            state[int(fixture_id)] = (
                None if task_status is None else str(task_status),
                dict(params or {}),
                match_status,
                elapsed,
            )
        return state

    async def _refresh_detail(
        self,
        fixture_id: int,
        *,
        final: bool,
        match_status: str | None,
        refresh_seconds: int = LIVE_DETAIL_REFRESH_SECONDS,
    ) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                task = await session.scalar(
                    select(EtlTask)
                    .where(EtlTask.endpoint == "/fixtures")
                    .where(EtlTask.fixture_id == fixture_id)
                    .where(EtlTask.cursor_kind.is_(None))
                    .order_by(EtlTask.id)
                    .limit(1)
                )
                if not detail_refresh_due(
                    task_status=None if task is None else task.status,
                    params=None if task is None else task.params,
                    match_status=match_status,
                    now=self._now(),
                    final=final,
                    refresh_seconds=refresh_seconds,
                ):
                    return False
                if task is None:
                    await ensure_enrichment_task(session, fixture_id)
                    await session.flush()
                elif task.status == "in_progress":
                    return False
                elif task.status not in {"pending", "retryable_error"}:
                    task.status = "pending"
                    task.completed_at = None
                    task.last_error = None
                    task.updated_at = self._now()
                    await session.flush()
                claimed = await claim_detail(
                    session, "/fixtures", [fixture_id], now=self._now()
                )
                task_id = None if claimed is None else int(claimed.id)
        if task_id is None:
            return False
        ok = await self._enrichment._enrich_task(task_id, control=False)
        if ok:
            finished = final or match_status in FINISHED_FIXTURE_STATUSES
            await self._stamp_detail(fixture_id, finished=finished)
        return ok

    async def _stamp_detail(self, fixture_id: int, *, finished: bool) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                task = await session.scalar(
                    select(EtlTask)
                    .where(EtlTask.endpoint == "/fixtures")
                    .where(EtlTask.fixture_id == fixture_id)
                    .where(EtlTask.cursor_kind.is_(None))
                    .order_by(EtlTask.id)
                    .limit(1)
                )
                if task is None:
                    return
                params = dict(task.params or {})
                stamp = self._now().isoformat()
                params["last_live_detail_at"] = stamp
                if finished:
                    params["final_detail_at"] = stamp
                task.params = params
                task.updated_at = self._now()

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


def _detail_row(
    fixture_id: int,
    stored: tuple[str | None, dict[str, Any], str | None, int | None] | None,
) -> tuple[int, str | None, int | None, datetime | None]:
    if stored is None:
        return fixture_id, None, None, None
    _task_status, params, match_status, elapsed = stored
    raw = (params or {}).get("last_live_detail_at") or (params or {}).get(
        "ft_refresh_at"
    )
    return fixture_id, match_status, elapsed, _parse_stamp(raw)


def _parse_stamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
