"""W8 global catalog: standings, teams, players, coachs, transfers. Runtime P8."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.client.errors import (
    AuthBlockedError,
    FootballHttpError,
    QuotaExhaustedError,
    RetryableHttpError,
    SpendWindowClosed,
)
from predictor.client.football import FootballClient
from predictor.constants import (
    LOOKUP_WITHOUT_HTTP,
    TEAM_STATS_SENTINEL_DATE,
    TOP_PLAYER_ENDPOINTS,
    lane_split,
)
from predictor.logutil import log_json
from predictor.models.catalog import LeagueSeason
from predictor.models.etl import EtlTask
from predictor.schemas.global_entities import (
    CareerTeamItem,
    CoachItem,
    PersonCore,
    PlayerBundle,
    SidelinedItem,
    SquadItem,
    StandingItem,
    TeamEnvelope,
    TeamStatisticsItem,
    TransferBundle,
    TrophyItem,
    VenueFull,
)
from predictor.services.ingest.drift import warn_model_extra
from predictor.services.ingest.paging import fetch_all_pages
from predictor.services.ingest.persist_people import (
    persist_coaches,
    persist_player_bundles,
    persist_player_career,
    persist_sidelined,
    persist_squads,
    persist_transfers,
    persist_trophies,
)
from predictor.services.ingest.persist_seasonal import (
    as_int,
    persist_standings,
    persist_team_statistics,
    persist_teams,
    sentinel_date,
    upsert_venues_full,
)
from predictor.services.ingest.task_completion import (
    fail_task,
    finish_task,
    release_in_progress_task,
    retry_or_empty_task,
)
from predictor.services.queue import (
    claim_global_batch,
    claim_live_global_task,
    complete_task,
    enqueue_coach_catalog,
    enqueue_global_for_match,
    enqueue_player_facts,
    enqueue_player_profile_if_needed,
    ensure_param_task,
    requeue_stale_global,
    skip_reconstructable_lookups,
)

_COVERAGE_BY_ENDPOINT: dict[str, str] = {
    "/standings": "cov_standings",
    "/players": "cov_players",
    "/players/topscorers": "cov_top_scorers",
    "/players/topassists": "cov_top_assists",
    "/players/topyellowcards": "cov_top_cards",
    "/players/topredcards": "cov_top_cards",
}

_PLAYER_ENDPOINTS = frozenset({"/players", "/players/profiles", *TOP_PLAYER_ENDPOINTS})
# /players is dozens of pages. One page per turn lets the live poll run between them.
_PLAYERS_PAGES_PER_TURN = 1


class GlobalIngest:
    def __init__(
        self,
        client: FootballClient,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now_fn: Callable[[], datetime] | None = None,
        per_tick: int | None = None,
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
        """One batch per tick for every global endpoint, not only /players.

        The scheduler returns to the live poll before the next history batch.
        /players also stores a single page per task (see _PLAYERS_PAGES_PER_TURN).
        """
        if not self._quota_left():
            return
        await self._requeue_stale()
        await self._skip_lookups()
        if not self._quota_left():
            return
        room = min(self._fetch_limit(), self._concurrency())
        if room <= 0:
            return
        task_ids = await self._claim_ids(room)
        if not task_ids:
            return
        await self._run_batch(task_ids)

    def _concurrency(self) -> int:
        if self._per_tick is not None:
            return self._per_tick
        value = getattr(self._client, "history_concurrency", None)
        if isinstance(value, int) and value > 0:
            return value
        return lane_split(0).history_concurrency

    def _fetch_limit(self) -> int:
        limit = self._concurrency()
        room = getattr(self._client, "history_room", None)
        if callable(room):
            available = room()
            if isinstance(available, int):
                limit = min(limit, available)
        return max(0, limit)

    async def _run_batch(self, task_ids: list[int]) -> None:
        async def run(task_id: int) -> None:
            try:
                await self._one_task(task_id=task_id)
            except asyncio.CancelledError:
                await release_in_progress_task(self._session_factory, task_id)
                raise

        results = await asyncio.gather(
            *(run(task_id) for task_id in task_ids),
            return_exceptions=True,
        )
        auth = next((exc for exc in results if isinstance(exc, AuthBlockedError)), None)
        if auth is not None:
            raise auth
        closed = next(
            (exc for exc in results if isinstance(exc, SpendWindowClosed)), None
        )
        if closed is not None:
            raise closed

    async def _requeue_stale(self) -> None:
        async with self._session_factory() as session, session.begin():
            await requeue_stale_global(session, self._now())

    async def _skip_lookups(self) -> None:
        async with self._session_factory() as session, session.begin():
            await skip_reconstructable_lookups(session)

    async def ingest_one_scoped(
        self, needles: Sequence[tuple[str, dict[str, Any]]]
    ) -> bool:
        if not needles or not self._quota_left():
            return False
        task_id = await self._claim_scoped_id(needles)
        if task_id is None:
            return False
        await self._one_task(task_id=task_id, retry_empty=True)
        return True

    async def _one_task(
        self, *, task_id: int | None = None, retry_empty: bool = False
    ) -> bool:
        if task_id is None:
            claimed = await self._claim_ids(1)
            task_id = claimed[0] if claimed else None
        if task_id is None:
            return False
        loaded = await self._load_task(task_id)
        if loaded is None:
            await self._fail(task_id, "permanent_error", "missing_task")
            return True
        task_id, endpoint, params = loaded
        if endpoint in LOOKUP_WITHOUT_HTTP:
            await self._finish(
                task_id, "not_supported", params={"reason": "reconstructable_lookup"}
            )
            return True
        if await self._coverage_blocked(endpoint, params) and not (
            retry_empty and endpoint == "/standings"
        ):
            if retry_empty:
                await self._retry_or_empty(
                    task_id, {**params, "reason": "coverage_false"}
                )
            else:
                await self._finish(
                    task_id,
                    "coverage_empty",
                    params={**params, "reason": "coverage_false"},
                )
            return True
        query = _http_params(endpoint, params)
        start_page = _resume_page(endpoint, params)
        if query is None:
            if _non_positive_identity(endpoint, params):
                await self._finish(
                    task_id,
                    "not_supported",
                    params={**params, "reason": "non_positive_id"},
                )
            else:
                await self._fail(task_id, "permanent_error", "bad_params")
            return True
        try:
            items, current, total = await fetch_all_pages(
                self._client,
                endpoint,
                params=query,
                start_page=start_page,
                max_pages=(_PLAYERS_PAGES_PER_TURN if endpoint == "/players" else None),
            )
        except AuthBlockedError:
            raise
        except SpendWindowClosed:
            await release_in_progress_task(self._session_factory, task_id)
            raise
        except QuotaExhaustedError:
            await self._fail(task_id, "retryable_error", "quota_exhausted")
            return True
        except (RetryableHttpError, FootballHttpError) as exc:
            await self._fail(task_id, "retryable_error", _http_error_label(exc))
            return True
        if not items:
            if start_page > 1 and endpoint == "/players":
                await self._release_player_page(
                    task_id, params, current=current, total=total
                )
                return True
            if retry_empty:
                await self._retry_or_empty(
                    task_id,
                    {**params, "reason": "empty"},
                    paging_current=current,
                    paging_total=total,
                )
            else:
                await self._finish(
                    task_id,
                    "coverage_empty",
                    params=params,
                    paging_current=current,
                    paging_total=total,
                )
            return True
        try:
            async with self._session_factory() as session, session.begin():
                task = await session.get(EtlTask, task_id)
                if task is None:
                    return True
                extra = await self._persist(session, endpoint, params, items)
                merged = dict(task.params)
                merged.update(extra)
                more_players = endpoint == "/players" and current < total and total > 0
                if more_players:
                    merged["page"] = current + 1
                    status = "pending"
                else:
                    merged.pop("page", None)
                    status = "complete"
                await complete_task(
                    session,
                    task,
                    status,
                    params=merged,
                    paging_current=current,
                    paging_total=total,
                )
        except Exception as exc:
            log_json(
                logging.ERROR,
                service="worker",
                event="global_persist_failed",
                endpoint=endpoint,
                error=type(exc).__name__,
            )
            await self._fail(task_id, "retryable_error", type(exc).__name__)
        return True

    async def _persist(
        self,
        session: AsyncSession,
        endpoint: str,
        params: dict[str, Any],
        items: list[Any],
    ) -> dict[str, Any]:
        if endpoint == "/standings":
            parsed = _parse_many(items, StandingItem, endpoint)
            extra = await persist_standings(session, parsed)
            league = as_int(params.get("league"))
            season = as_int(params.get("season"))
            if league is not None and season is not None:
                await enqueue_global_for_match(
                    session,
                    league_id=league,
                    season=season,
                    team_ids=tuple(int(tid) for tid in extra.get("team_ids", [])),
                )
            return {"count": extra.get("count", 0)}
        if endpoint == "/teams":
            parsed_teams = _parse_many(items, TeamEnvelope, endpoint)
            extra = await persist_teams(session, parsed_teams)
            for venue_id in extra.get("venue_refresh_ids", []):
                await ensure_param_task(session, "/venues", {"id": int(venue_id)})
            return extra
        if endpoint == "/venues":
            parsed_venues = _parse_many(items, VenueFull, endpoint)
            count = await upsert_venues_full(session, parsed_venues)
            return {"count": count}
        if endpoint == "/teams/statistics":
            parsed_stats = _parse_many(items, TeamStatisticsItem, endpoint)
            as_of = _as_of_date(params)
            count = await persist_team_statistics(
                session,
                parsed_stats,
                as_of=as_of,
                fallback_team=as_int(params.get("team")),
                fallback_league=as_int(params.get("league")),
                fallback_season=as_int(params.get("season")),
            )
            return {"count": count, "as_of_date": as_of.isoformat()}
        if endpoint in _PLAYER_ENDPOINTS:
            parsed_players = _parse_player_items(items, endpoint)
            extra = await persist_player_bundles(session, parsed_players)
            if endpoint == "/players":
                for player_id in extra.get("player_ids", []):
                    await enqueue_player_facts(session, int(player_id))
            return {
                "players": extra.get("players", 0),
                "statistics": extra.get("statistics", 0),
            }
        if endpoint == "/players/squads":
            parsed_squads = _parse_many(items, SquadItem, endpoint)
            count = await persist_squads(session, parsed_squads)
            for item in parsed_squads:
                for player in item.players:
                    if player.id is not None:
                        await enqueue_player_facts(session, player.id)
                        await enqueue_player_profile_if_needed(session, player.id)
            return {"count": count}
        if endpoint == "/players/teams":
            player_id = as_int(params.get("player"))
            if player_id is None:
                return {"count": 0}
            parsed_career = _parse_many(items, CareerTeamItem, endpoint)
            count = await persist_player_career(session, player_id, parsed_career)
            return {"count": count}
        if endpoint == "/coachs":
            parsed_coaches = _parse_many(items, CoachItem, endpoint)
            count = await persist_coaches(session, parsed_coaches)
            for coach in parsed_coaches:
                await enqueue_coach_catalog(session, coach.id)
            return {"count": count}
        if endpoint == "/transfers":
            parsed_tx = _parse_many(items, TransferBundle, endpoint)
            count = await persist_transfers(session, parsed_tx)
            return {"count": count}
        if endpoint == "/trophies":
            parsed_tr = _parse_many(items, TrophyItem, endpoint)
            count = await persist_trophies(
                session,
                parsed_tr,
                player_id=as_int(params.get("player")),
                coach_id=as_int(params.get("coach")),
            )
            return {"count": count}
        if endpoint == "/sidelined":
            parsed_sl = _parse_many(items, SidelinedItem, endpoint)
            count = await persist_sidelined(
                session,
                parsed_sl,
                player_id=as_int(params.get("player")),
                coach_id=as_int(params.get("coach")),
            )
            return {"count": count}
        return {"count": 0}

    async def _claim_ids(self, limit: int) -> list[int]:
        async with self._session_factory() as session, session.begin():
            tasks = await claim_global_batch(session, limit)
            return [int(task.id) for task in tasks]

    async def _claim_scoped_id(
        self, needles: Sequence[tuple[str, dict[str, Any]]]
    ) -> int | None:
        async with self._session_factory() as session, session.begin():
            task = await claim_live_global_task(session, needles, now=self._now())
            return None if task is None else int(task.id)

    async def _retry_or_empty(
        self,
        task_id: int,
        params: dict[str, Any],
        *,
        paging_current: int | None = None,
        paging_total: int | None = None,
    ) -> None:
        await retry_or_empty_task(
            self._session_factory,
            task_id,
            params,
            now=self._now(),
            paging_current=paging_current,
            paging_total=paging_total,
        )

    async def _load_task(self, task_id: int) -> tuple[int, str, dict[str, Any]] | None:
        async with self._session_factory() as session:
            task = await session.get(EtlTask, task_id)
            if task is None:
                return None
            return int(task.id), task.endpoint, dict(task.params)

    async def _coverage_blocked(self, endpoint: str, params: dict[str, Any]) -> bool:
        flag_name = _COVERAGE_BY_ENDPOINT.get(endpoint)
        if flag_name is None:
            return False
        league_id = as_int(params.get("league"))
        season = as_int(params.get("season"))
        if league_id is None or season is None:
            return False
        async with self._session_factory() as session:
            row = await session.get(LeagueSeason, (league_id, season))
        if row is None:
            return False
        return getattr(row, flag_name) is False

    async def _finish(
        self,
        task_id: int,
        status: str,
        *,
        params: dict[str, Any] | None = None,
        paging_current: int | None = None,
        paging_total: int | None = None,
    ) -> None:
        await finish_task(
            self._session_factory,
            task_id,
            status,
            params=params,
            paging_current=paging_current,
            paging_total=paging_total,
        )

    async def _fail(self, task_id: int, status: str, error: str) -> None:
        await fail_task(self._session_factory, task_id, status, error)

    async def _release_player_page(
        self,
        task_id: int,
        params: dict[str, Any],
        *,
        current: int,
        total: int,
    ) -> None:
        """Keep a resumed /players list pending when this page had no rows."""
        if current < total:
            await self._finish(
                task_id,
                "pending",
                params={**params, "page": current + 1},
                paging_current=current,
                paging_total=total,
            )
            return
        done = {key: value for key, value in params.items() if key != "page"}
        await self._finish(
            task_id,
            "complete",
            params=done,
            paging_current=current,
            paging_total=total,
        )


def _as_of_date(params: dict[str, Any]) -> date:
    raw_date = params.get("date")
    if isinstance(raw_date, str) and raw_date and raw_date != TEAM_STATS_SENTINEL_DATE:
        try:
            return date.fromisoformat(raw_date)
        except ValueError:
            return sentinel_date()
    return sentinel_date()


def _http_error_label(exc: FootballHttpError | RetryableHttpError) -> str:
    """Compact last_error for HTTP failures: class name plus status when known."""
    status = exc.status_code
    name = type(exc).__name__
    if status is None:
        return name
    return f"{name}: {status}"


def _positive_id(raw: object | None) -> int | None:
    value = as_int(raw)
    if value is None or value <= 0:
        return None
    return value


def _non_positive_identity(endpoint: str, params: dict[str, Any]) -> bool:
    """True when the task names an id the vendor cannot look up."""
    if endpoint in {"/trophies", "/sidelined"}:
        present = [params[key] for key in ("player", "coach") if key in params]
        if not present:
            return False
        non_positive = False
        for raw in present:
            if _positive_id(raw) is not None:
                return False
            parsed = as_int(raw)
            if parsed is not None and parsed <= 0:
                non_positive = True
        return non_positive
    key = {
        "/players/profiles": "player",
        "/players/teams": "player",
        "/transfers": "player",
        "/players/squads": "team",
        "/teams/statistics": "team",
        "/teams": "id",
        "/venues": "id",
        "/coachs": "id",
    }.get(endpoint)
    if key is None:
        return False
    parsed = as_int(params.get(key))
    return parsed is not None and parsed <= 0


def _resume_page(endpoint: str, params: dict[str, Any]) -> int:
    if endpoint != "/players":
        return 1
    page = as_int(params.get("page"))
    if page is None or page < 1:
        return 1
    return page


def _http_params(endpoint: str, params: dict[str, Any]) -> dict[str, str | int] | None:
    if endpoint in {"/standings", "/players", *TOP_PLAYER_ENDPOINTS}:
        league = as_int(params.get("league"))
        season = as_int(params.get("season"))
        if league is None or season is None:
            return None
        return {"league": league, "season": season}
    if endpoint == "/teams/statistics":
        team = _positive_id(params.get("team"))
        league = as_int(params.get("league"))
        season = as_int(params.get("season"))
        if team is None or league is None or season is None:
            return None
        query: dict[str, str | int] = {
            "team": team,
            "league": league,
            "season": season,
        }
        raw_date = params.get("date")
        if (
            isinstance(raw_date, str)
            and raw_date
            and raw_date != TEAM_STATS_SENTINEL_DATE
        ):
            query["date"] = raw_date
        return query
    if endpoint in {"/teams", "/venues", "/coachs"}:
        ident = _positive_id(params.get("id"))
        if ident is None:
            return None
        return {"id": ident}
    if endpoint in {"/players/profiles", "/players/teams", "/transfers"}:
        player = _positive_id(params.get("player"))
        if player is None:
            return None
        return {"player": player}
    if endpoint == "/players/squads":
        team = _positive_id(params.get("team"))
        if team is None:
            return None
        return {"team": team}
    if endpoint in {"/trophies", "/sidelined"}:
        player = _positive_id(params.get("player"))
        coach = _positive_id(params.get("coach"))
        if player is not None and coach is None:
            return {"player": player}
        if coach is not None and player is None:
            return {"coach": coach}
        return None
    return None


def _parse_many[T: BaseModel](raw: list[Any], model: type[T], endpoint: str) -> list[T]:
    items: list[T] = []
    for row in raw:
        try:
            item = model.model_validate(row)
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


def _parse_player_items(raw: list[Any], endpoint: str) -> list[PlayerBundle]:
    items: list[PlayerBundle] = []
    for row in raw:
        try:
            if isinstance(row, dict) and "player" in row:
                item = PlayerBundle.model_validate(row)
            else:
                person = PersonCore.model_validate(row)
                item = PlayerBundle(player=person)
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
