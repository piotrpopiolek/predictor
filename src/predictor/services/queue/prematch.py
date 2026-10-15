"""Prematch claims: H2H, predictions, odds."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.services.queue.core import (
    _claim_endpoint,
    _claim_fixture_endpoint,
    _require_transaction,
    drop_untracked_fixture_ids,
    retire_untracked_tasks,
)
from predictor.services.queue.enrichment import ensure_enrichment_task


async def ensure_prematch_for_fixture_ids(
    session: AsyncSession, fixture_ids: list[int]
) -> None:
    _require_transaction(session, "ensure prematch for fixtures")
    await retire_untracked_tasks(session)
    ids = await drop_untracked_fixture_ids(session, fixture_ids)
    ids = sorted(ids)
    if not ids:
        return
    rows = await session.execute(
        select(Fixture.id, Fixture.home_team_id, Fixture.away_team_id).where(
            Fixture.id.in_(ids)
        )
    )
    for fixture_id, home_id, away_id in rows:
        await ensure_enrichment_task(session, int(fixture_id))
        await ensure_h2h_task(session, int(home_id), int(away_id))
        await ensure_predictions_task(session, int(fixture_id))
        await ensure_odds_task(session, int(fixture_id))


def h2h_param(home_id: int, away_id: int) -> str:
    return f"{home_id}-{away_id}"


async def ensure_h2h_task(session: AsyncSession, home_id: int, away_id: int) -> None:
    _require_transaction(session, "ensure h2h")
    params = {"h2h": h2h_param(home_id, away_id)}
    existing = await session.scalar(
        select(EtlTask.id)
        .where(EtlTask.endpoint == "/fixtures/headtohead")
        .where(EtlTask.params.contains(params))
        .where(EtlTask.cursor_kind.is_(None))
    )
    if existing is None:
        session.add(
            EtlTask(
                endpoint="/fixtures/headtohead",
                params=params,
                status="pending",
            )
        )


async def ensure_predictions_task(session: AsyncSession, fixture_id: int) -> None:
    _require_transaction(session, "ensure predictions")
    existing = await session.scalar(
        select(EtlTask.id)
        .where(EtlTask.endpoint == "/predictions")
        .where(EtlTask.fixture_id == fixture_id)
        .where(EtlTask.cursor_kind.is_(None))
    )
    if existing is None:
        session.add(
            EtlTask(
                endpoint="/predictions",
                fixture_id=fixture_id,
                params={"fixture": fixture_id},
                status="pending",
            )
        )


async def ensure_odds_task(session: AsyncSession, fixture_id: int) -> None:
    _require_transaction(session, "ensure odds")
    existing = await session.scalar(
        select(EtlTask.id)
        .where(EtlTask.endpoint == "/odds")
        .where(EtlTask.fixture_id == fixture_id)
        .where(EtlTask.cursor_kind.is_(None))
    )
    if existing is None:
        session.add(
            EtlTask(
                endpoint="/odds",
                fixture_id=fixture_id,
                params={"fixture": fixture_id},
                status="pending",
            )
        )


async def claim_h2h_task(session: AsyncSession) -> EtlTask | None:
    _require_transaction(session, "claim h2h")
    return await _claim_endpoint(session, "/fixtures/headtohead")


async def claim_predictions_task(session: AsyncSession) -> EtlTask | None:
    _require_transaction(session, "claim predictions")
    return await _claim_fixture_endpoint(session, "/predictions", urgent=False)


async def claim_odds_task(session: AsyncSession) -> EtlTask | None:
    _require_transaction(session, "claim odds")
    return await _claim_fixture_endpoint(session, "/odds", urgent=False)


async def claim_urgent_predictions_task(
    session: AsyncSession, *, now: datetime | None = None
) -> EtlTask | None:
    _require_transaction(session, "claim urgent predictions")
    return await _claim_fixture_endpoint(session, "/predictions", urgent=True, now=now)


async def claim_urgent_odds_task(
    session: AsyncSession, *, now: datetime | None = None
) -> EtlTask | None:
    _require_transaction(session, "claim urgent odds")
    return await _claim_fixture_endpoint(session, "/odds", urgent=True, now=now)
