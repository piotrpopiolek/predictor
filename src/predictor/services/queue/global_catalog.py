"""Global catalog claims: standings, people, coaches, venues."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from typing import cast as cast_type

from sqlalchemy import Integer, cast, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.constants import (
    GLOBAL_ENDPOINT_ORDER,
    LOOKUP_WITHOUT_HTTP,
    STALE_GLOBAL_ENDPOINTS,
    TOP_PLAYER_ENDPOINTS,
)
from predictor.models.catalog import Player
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Team, Venue
from predictor.services.queue.core import (
    _claim_endpoint,
    _claim_endpoint_rows,
    _require_transaction,
    complete_task,
    ensure_param_task,
    needs_refresh,
)
from predictor.services.queue.live import claim_live_param_task


async def enqueue_global_for_match(
    session: AsyncSession,
    *,
    league_id: int,
    season: int,
    team_ids: tuple[int, ...],
) -> None:
    _require_transaction(session, "enqueue global")
    league_params = {"league": league_id, "season": season}
    await ensure_param_task(session, "/standings", league_params)
    await ensure_param_task(session, "/players", league_params)
    for endpoint in TOP_PLAYER_ENDPOINTS:
        await ensure_param_task(session, endpoint, league_params)
    for team_id in dict.fromkeys(team_ids):
        await ensure_param_task(session, "/teams", {"id": team_id})
        await ensure_param_task(session, "/players/squads", {"team": team_id})
        await ensure_param_task(
            session,
            "/teams/statistics",
            {"team": team_id, "league": league_id, "season": season},
        )


_PLAYER_FACT_ENDPOINTS = (
    "/players/teams",
    "/transfers",
    "/trophies",
    "/sidelined",
)


async def enqueue_player_facts(session: AsyncSession, player_id: int) -> None:
    """Queue career, transfers, trophies, and injuries. Not the profile."""
    _require_transaction(session, "enqueue player facts")
    params = {"player": player_id}
    for endpoint in _PLAYER_FACT_ENDPOINTS:
        await ensure_param_task(session, endpoint, params)


async def enqueue_player_profile_if_needed(
    session: AsyncSession, player_id: int
) -> None:
    """Queue /players/profiles only when the row is missing or still a stub."""
    _require_transaction(session, "enqueue player profile")
    nationality = await session.scalar(
        select(Player.nationality).where(Player.id == player_id)
    )
    if nationality is not None:
        return
    await ensure_param_task(session, "/players/profiles", {"player": player_id})


async def complete_stored_player_profiles(session: AsyncSession) -> int:
    """Close pending /players/profiles when players already has a full profile.

    A full profile is a row with nationality set. Missing players and squad stubs
    stay pending. Tasks that carry params.coach are left alone.
    """
    _require_transaction(session, "complete stored player profiles")
    now = datetime.now(UTC)
    player_id = cast(EtlTask.params["player"].astext, Integer)
    result = await session.execute(
        update(EtlTask)
        .where(EtlTask.endpoint == "/players/profiles")
        .where(EtlTask.status == "pending")
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.params.has_key("player"))
        .where(~EtlTask.params.has_key("coach"))
        .where(player_id.in_(select(Player.id).where(Player.nationality.is_not(None))))
        .values(status="complete", updated_at=now, completed_at=now)
        .execution_options(synchronize_session=False)
    )
    updated = cast_type(CursorResult[Any], result)
    return int(updated.rowcount or 0)


async def complete_stored_venues(session: AsyncSession) -> int:
    """Close pending /venues when /teams already stored the stadium.

    Country is copied from the only team that uses the ground. Stadiums shared
    by teams from different countries stay pending: only /venues knows which
    country the ground itself belongs to.
    """
    _require_transaction(session, "complete stored venues")
    single_country = (
        select(
            Team.venue_id.label("venue_id"),
            func.min(Team.country_name).label("country_name"),
        )
        .where(Team.venue_id.is_not(None))
        .where(Team.country_name.is_not(None))
        .group_by(Team.venue_id)
        .having(func.count(func.distinct(Team.country_name)) == 1)
        .subquery()
    )
    await session.execute(
        update(Venue)
        .where(Venue.country_name.is_(None))
        .where(Venue.id == single_country.c.venue_id)
        .values(country_name=single_country.c.country_name)
        .execution_options(synchronize_session=False)
    )
    now = datetime.now(UTC)
    venue_param = cast(EtlTask.params["id"].astext, Integer)
    several_countries = (
        select(Team.venue_id)
        .where(Team.venue_id.is_not(None))
        .where(Team.country_name.is_not(None))
        .group_by(Team.venue_id)
        .having(func.count(func.distinct(Team.country_name)) > 1)
    )
    result = await session.execute(
        update(EtlTask)
        .where(EtlTask.endpoint == "/venues")
        .where(EtlTask.status == "pending")
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.params.has_key("id"))
        .where(EtlTask.params["id"].astext.op("~")(r"^[0-9]+$"))
        .where(venue_param.in_(select(Venue.id).where(Venue.country_name.is_not(None))))
        .where(venue_param.not_in(several_countries))
        .values(status="complete", updated_at=now, completed_at=now)
        .execution_options(synchronize_session=False)
    )
    updated = cast_type(CursorResult[Any], result)
    return int(updated.rowcount or 0)


async def enqueue_coach_catalog(session: AsyncSession, coach_id: int) -> None:
    _require_transaction(session, "enqueue coach catalog")
    await ensure_param_task(session, "/coachs", {"id": coach_id})
    await ensure_param_task(session, "/trophies", {"coach": coach_id})
    await ensure_param_task(session, "/sidelined", {"coach": coach_id})


async def claim_global_task(session: AsyncSession) -> EtlTask | None:
    rows = await claim_global_batch(session, 1)
    return rows[0] if rows else None


async def claim_global_batch(session: AsyncSession, limit: int) -> list[EtlTask]:
    """Claim up to ``limit`` due rows of the first global endpoint that has any.

    One transaction marks them in_progress. A later batch skips those rows.
    """
    _require_transaction(session, "claim global batch")
    if limit <= 0:
        return []
    for endpoint in GLOBAL_ENDPOINT_ORDER:
        rows = await _claim_endpoint_rows(session, endpoint, limit=limit)
        if rows:
            return rows
    return []


async def claim_live_global_task(
    session: AsyncSession,
    needles: Sequence[tuple[str, dict[str, Any]]],
    *,
    now: datetime | None = None,
) -> EtlTask | None:
    _require_transaction(session, "claim live global")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for endpoint, params in needles:
        grouped.setdefault(endpoint, []).append(params)
    for endpoint in GLOBAL_ENDPOINT_ORDER:
        params_list = grouped.get(endpoint)
        if not params_list:
            continue
        task = await claim_live_param_task(session, endpoint, params_list, now=now)
        if task is not None:
            return task
    return None


async def requeue_stale_global(session: AsyncSession, now: datetime) -> int:
    _require_transaction(session, "requeue stale global")
    if not STALE_GLOBAL_ENDPOINTS:
        return 0
    count = 0
    tasks = await session.scalars(
        select(EtlTask)
        .where(EtlTask.endpoint.in_(tuple(STALE_GLOBAL_ENDPOINTS)))
        .where(EtlTask.status == "complete")
        .where(EtlTask.cursor_kind.is_(None))
    )
    for task in tasks:
        if needs_refresh(task, now):
            task.status = "pending"
            task.created_at = now
            task.updated_at = now
            task.completed_at = None
            count += 1
    return count


async def skip_reconstructable_lookups(session: AsyncSession) -> int:
    _require_transaction(session, "skip lookups")
    count = 0
    for endpoint in LOOKUP_WITHOUT_HTTP:
        while True:
            task = await _claim_endpoint(session, endpoint)
            if task is None:
                break
            await complete_task(
                session,
                task,
                "not_supported",
                error="reconstructable_lookup",
            )
            count += 1
    return count
