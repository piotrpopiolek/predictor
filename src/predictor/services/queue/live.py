"""Live-board claims scoped to in-play and finishing fixtures."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.services.queue.core import (
    _due_clause,
    _mark_claimed,
    _require_transaction,
)
from predictor.services.queue.enrichment import ensure_enrichment_task
from predictor.services.queue.prematch import h2h_param


async def get_or_create_live_fixtures_task(session: AsyncSession) -> EtlTask:
    _require_transaction(session, "get or create live fixtures")
    params = {"live": "all"}
    task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.fixture_id.is_(None))
        .where(EtlTask.day_utc.is_(None))
        .where(EtlTask.params.contains(params))
        .order_by(EtlTask.id)
        .limit(1)
    )
    if task is None:
        task = EtlTask(
            endpoint="/fixtures",
            params=params,
            status="pending",
        )
        session.add(task)
        await session.flush()
    return task


async def ensure_live_followups(
    session: AsyncSession, fixture_ids: Sequence[int]
) -> None:
    """Enqueue context tasks for the given fixtures (idempotent)."""
    _require_transaction(session, "ensure live followups")
    ids = list(dict.fromkeys(int(fid) for fid in fixture_ids))
    if not ids:
        return
    rows = (
        await session.execute(
            select(
                Fixture.id,
                Fixture.home_team_id,
                Fixture.away_team_id,
                Fixture.league_id,
                Fixture.season,
            ).where(Fixture.id.in_(ids))
        )
    ).all()
    discovered: list[dict[str, int]] = []
    seasons: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    for fixture_id, home_id, away_id, league_id, season in rows:
        discovered.append(
            {
                "id": int(fixture_id),
                "home": int(home_id),
                "away": int(away_id),
                "league": int(league_id),
                "season": int(season),
            }
        )
        key = (int(league_id), int(season))
        if key not in seen:
            seen.add(key)
            seasons.append({"league": key[0], "season": key[1]})
    if discovered:
        from predictor.services.queue.followups import enqueue_fixture_followups

        await enqueue_fixture_followups(
            session, {"discovered": discovered, "league_seasons": seasons}
        )


async def reopen_live_detail_task(session: AsyncSession, fixture_id: int) -> None:
    """Make /fixtures?id= claimable again so a live match can refresh this tick."""
    _require_transaction(session, "reopen live detail")
    task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.fixture_id == fixture_id)
        .where(EtlTask.cursor_kind.is_(None))
        .order_by(EtlTask.id)
        .limit(1)
    )
    if task is None:
        await ensure_enrichment_task(session, fixture_id)
        return
    if task.status in {"pending", "retryable_error", "in_progress"}:
        return
    task.status = "pending"
    task.completed_at = None
    task.last_error = None
    task.updated_at = datetime.now(UTC)


async def claim_live_fixture_task(
    session: AsyncSession,
    endpoint: str,
    fixture_ids: Sequence[int],
    *,
    now: datetime | None = None,
) -> EtlTask | None:
    """Claim one open task whose fixture is in the live/finishing set."""
    _require_transaction(session, "claim live fixture task")
    ids = list(dict.fromkeys(int(fid) for fid in fixture_ids))
    if not ids:
        return None
    stamp = now or datetime.now(UTC)
    priority = case(
        *[(EtlTask.fixture_id == fid, index) for index, fid in enumerate(ids)],
        else_=len(ids),
    )
    stmt = (
        select(EtlTask)
        .where(EtlTask.endpoint == endpoint)
        .where(EtlTask.fixture_id.in_(ids))
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.status.in_(("pending", "retryable_error")))
        .where(_due_clause(stamp))
        .order_by(priority, EtlTask.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    task = await session.scalar(stmt)
    if task is None:
        return None
    return _mark_claimed(task)


async def claim_live_param_task(
    session: AsyncSession,
    endpoint: str,
    param_sets: Sequence[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> EtlTask | None:
    _require_transaction(session, "claim live param task")
    if not param_sets:
        return None
    stamp = now or datetime.now(UTC)
    clauses = [EtlTask.params.contains(params) for params in param_sets]
    stmt = (
        select(EtlTask)
        .where(EtlTask.endpoint == endpoint)
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.status.in_(("pending", "retryable_error")))
        .where(_due_clause(stamp))
        .where(or_(*clauses))
        .order_by(EtlTask.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    task = await session.scalar(stmt)
    if task is None:
        return None
    return _mark_claimed(task)


async def unfinished_context_fixture_ids(
    session: AsyncSession, fixture_ids: Sequence[int]
) -> list[int]:
    """Fixtures that still have an open odds, predictions, or H2H task."""
    ids = list(dict.fromkeys(int(fid) for fid in fixture_ids))
    if not ids:
        return []
    rows = (
        await session.execute(
            select(Fixture.id, Fixture.home_team_id, Fixture.away_team_id).where(
                Fixture.id.in_(ids)
            )
        )
    ).all()
    teams = {
        int(fixture_id): (int(home_id), int(away_id))
        for fixture_id, home_id, away_id in rows
    }
    open_status = ("pending", "retryable_error", "in_progress")
    open_fixtures = set(
        await session.scalars(
            select(EtlTask.fixture_id).where(
                EtlTask.fixture_id.in_(ids),
                EtlTask.endpoint.in_(("/odds", "/predictions")),
                EtlTask.cursor_kind.is_(None),
                EtlTask.status.in_(open_status),
            )
        )
    )
    keys = [h2h_param(home_id, away_id) for home_id, away_id in teams.values()]
    open_h2h: set[str] = set()
    if keys:
        h2h_rows = (
            await session.scalars(
                select(EtlTask).where(
                    EtlTask.endpoint == "/fixtures/headtohead",
                    EtlTask.cursor_kind.is_(None),
                    EtlTask.status.in_(open_status),
                    or_(*[EtlTask.params.contains({"h2h": key}) for key in keys]),
                )
            )
        ).all()
        for task in h2h_rows:
            raw = task.params.get("h2h") if task.params else None
            if isinstance(raw, str):
                open_h2h.add(raw)
    still: list[int] = []
    for fixture_id in ids:
        pair = teams.get(fixture_id)
        if pair is None:
            continue
        if fixture_id in open_fixtures or h2h_param(*pair) in open_h2h:
            still.append(fixture_id)
    return still
