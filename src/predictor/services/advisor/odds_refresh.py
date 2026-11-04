"""Enqueue urgent live-odds pulls for the worker. Advisor-owned table."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.constants import ADVISOR_ODDS_REFRESH_CHANNEL
from predictor.models.advisor import AdvisorOddsRefresh


async def enqueue_odds_refresh(
    session: AsyncSession,
    fixture_ids: Sequence[int],
    *,
    now: datetime,
) -> tuple[int, ...]:
    """Upsert refresh requests and notify the worker. Returns sorted ids."""
    ids = tuple(sorted({int(fid) for fid in fixture_ids}))
    if not ids:
        return ()
    for fixture_id in ids:
        stmt = insert(AdvisorOddsRefresh).values(
            fixture_id=fixture_id, requested_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[AdvisorOddsRefresh.fixture_id],
            set_={"requested_at": now},
        )
        await session.execute(stmt)
    await session.execute(
        text("SELECT pg_notify(:channel, '')"),
        {"channel": ADVISOR_ODDS_REFRESH_CHANNEL},
    )
    return ids
