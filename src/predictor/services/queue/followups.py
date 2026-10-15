"""Fan-out from a discovered fixture into enrichment, prematch, and global."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from predictor.services.queue.core import (
    _require_transaction,
    drop_untracked_fixture_ids,
    ensure_rounds_task,
    historical_backlog_blocks,
    retire_untracked_tasks,
)
from predictor.services.queue.enrichment import (
    ensure_enrichment_task,
    ensure_injuries_task,
)
from predictor.services.queue.global_catalog import enqueue_global_for_match
from predictor.services.queue.prematch import (
    ensure_h2h_task,
    ensure_odds_task,
    ensure_predictions_task,
)


async def enqueue_fixture_followups(
    session: AsyncSession, extra: dict[str, Any], *, historical: bool = False
) -> None:
    _require_transaction(session, "enqueue fixture followups")
    ignored = await retire_untracked_tasks(session)
    if historical and await historical_backlog_blocks(session):
        return
    discovered = extra.get("discovered")
    if isinstance(discovered, list) and discovered:
        for row in discovered:
            if not isinstance(row, dict):
                continue
            try:
                fixture_id = int(row["id"])
                home_id = int(row["home"])
                away_id = int(row["away"])
            except (KeyError, TypeError, ValueError):
                continue
            league_id = row.get("league")
            if league_id is not None and int(league_id) in ignored:
                continue
            await ensure_enrichment_task(session, fixture_id)
            await ensure_h2h_task(session, home_id, away_id)
            await ensure_predictions_task(session, fixture_id)
            await ensure_odds_task(session, fixture_id)
            season = row.get("season")
            if league_id is not None and season is not None:
                await enqueue_global_for_match(
                    session,
                    league_id=int(league_id),
                    season=int(season),
                    team_ids=(home_id, away_id),
                )
    else:
        raw_ids = [int(raw_id) for raw_id in extra.get("fixture_ids", [])]
        kept = set(await drop_untracked_fixture_ids(session, raw_ids))
        for fixture_id in raw_ids:
            if fixture_id not in kept:
                continue
            await ensure_enrichment_task(session, fixture_id)
            await ensure_odds_task(session, fixture_id)
    for pair in extra.get("league_seasons", []):
        league_id = int(pair["league"])
        if league_id in ignored:
            continue
        season = int(pair["season"])
        await ensure_rounds_task(session, league_id, season)
        await ensure_injuries_task(session, league_id, season)
