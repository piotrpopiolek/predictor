"""Idempotent persist for /predictions. H2H fixtures are upserted before links."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.models.fixtures import Fixture
from predictor.models.predictions import Prediction, PredictionH2H
from predictor.schemas.fixtures import FixtureTeam
from predictor.schemas.predictions import PredictionItem
from predictor.services.ingest.persist_fixtures import upsert_fixtures, upsert_teams


def _as_str(raw: object | None, limit: int) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text[:limit]


async def persist_prediction(
    session: AsyncSession,
    fixture_id: int,
    item: PredictionItem,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    synced = now or datetime.now(UTC)
    extra = await upsert_fixtures(session, item.h2h)
    h2h_ids = [int(fid) for fid in extra.get("fixture_ids", [])]
    existing = set(
        await session.scalars(select(Fixture.id).where(Fixture.id.in_(h2h_ids)))
    )
    links: list[dict[str, Any]] = []
    for index, past in enumerate(item.h2h):
        h2h_id = past.fixture.id
        if h2h_id not in existing:
            continue
        links.append(
            {
                "fixture_id": fixture_id,
                "h2h_fixture_id": h2h_id,
                "sort_order": index,
            }
        )
    unique_links: dict[tuple[int, int], dict[str, Any]] = {}
    for row in links:
        unique_links[(row["fixture_id"], row["h2h_fixture_id"])] = row
    await session.execute(
        delete(PredictionH2H).where(PredictionH2H.fixture_id == fixture_id)
    )
    if unique_links:
        await session.execute(insert(PredictionH2H).values(list(unique_links.values())))

    block = item.predictions
    winner_id: int | None = None
    winner_comment: str | None = None
    win_or_draw: bool | None = None
    under_over: str | None = None
    goals_home: str | None = None
    goals_away: str | None = None
    advice: str | None = None
    pct_home: str | None = None
    pct_draw: str | None = None
    pct_away: str | None = None
    if block is not None:
        if block.winner is not None and block.winner.id is not None:
            winner_id = block.winner.id
            await upsert_teams(
                session,
                [FixtureTeam(id=winner_id, name=block.winner.name)],
            )
            winner_comment = _as_str(block.winner.comment, 10000)
        win_or_draw = block.win_or_draw
        under_over = _as_str(block.under_over, 16)
        if block.goals is not None:
            goals_home = _as_str(block.goals.home, 16)
            goals_away = _as_str(block.goals.away, 16)
        advice = _as_str(block.advice, 10000)
        if block.percent is not None:
            pct_home = _as_str(block.percent.home, 8)
            pct_draw = _as_str(block.percent.draw, 8)
            pct_away = _as_str(block.percent.away, 8)

    values = {
        "fixture_id": fixture_id,
        "winner_team_id": winner_id,
        "winner_comment": winner_comment,
        "win_or_draw": win_or_draw,
        "under_over": under_over,
        "goals_home": goals_home,
        "goals_away": goals_away,
        "advice": advice,
        "pct_home": pct_home,
        "pct_draw": pct_draw,
        "pct_away": pct_away,
        "comparison": item.comparison,
        "teams": item.teams,
        "synced_at": synced,
    }
    stmt = insert(Prediction).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Prediction.fixture_id],
        set_={
            key: getattr(stmt.excluded, key) for key in values if key != "fixture_id"
        },
    )
    await session.execute(stmt)
    return {"h2h": extra.get("count", 0), "links": len(unique_links)}


async def persist_predictions(
    session: AsyncSession,
    fixture_id: int,
    items: Sequence[PredictionItem],
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    if not items:
        return {"h2h": 0, "links": 0}
    return await persist_prediction(session, fixture_id, items[0], now=now)
