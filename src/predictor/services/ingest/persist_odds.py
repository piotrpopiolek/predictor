"""Idempotent persist for /odds (pre-match) and /odds/mapping.

Bet IDs go to the pre-match dictionary only — never the live snapshot table.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.models.catalog import Bookmaker, League, OddsBet
from predictor.models.fixtures import Fixture
from predictor.models.odds import FixtureOdds, OddsFixtureMapping
from predictor.schemas.catalog import NamedIdItem
from predictor.schemas.odds import OddsMappingItem, PrematchOddsItem
from predictor.services.ingest.persist import _chunks, upsert_named_ids

_VALUE_LABEL_MAX = 64


def _parse_odd(raw: object) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return value


async def persist_prematch_odds(
    session: AsyncSession, items: Sequence[PrematchOddsItem]
) -> dict[str, int]:
    fixture_ids = {item.fixture.id for item in items}
    existing = set()
    if fixture_ids:
        existing = set(
            await session.scalars(select(Fixture.id).where(Fixture.id.in_(fixture_ids)))
        )
    bookmakers: list[NamedIdItem] = []
    bets: list[NamedIdItem] = []
    rows: dict[tuple[int, int, int, str], dict[str, Any]] = {}
    skipped = 0
    for item in items:
        fixture_id = item.fixture.id
        if fixture_id not in existing:
            skipped += 1
            continue
        for bookmaker in item.bookmakers:
            bookmakers.append(NamedIdItem(id=bookmaker.id, name=bookmaker.name))
            for bet in bookmaker.bets:
                bets.append(NamedIdItem(id=bet.id, name=bet.name))
                for value in bet.values:
                    label = (value.value or "").strip()
                    if not label or len(label) > _VALUE_LABEL_MAX:
                        continue
                    odd = _parse_odd(value.odd)
                    if odd is None:
                        continue
                    rows[(fixture_id, bookmaker.id, bet.id, label)] = {
                        "fixture_id": fixture_id,
                        "bookmaker_id": bookmaker.id,
                        "bet_id": bet.id,
                        "value_label": label,
                        "odd": odd,
                        "api_update": item.update,
                    }
    await upsert_named_ids(session, Bookmaker, bookmakers)
    await upsert_named_ids(session, OddsBet, bets)
    stored = 0
    if rows:
        for chunk in _chunks(list(rows.values())):
            values = list(chunk)
            stmt = insert(FixtureOdds).values(values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    FixtureOdds.fixture_id,
                    FixtureOdds.bookmaker_id,
                    FixtureOdds.bet_id,
                    FixtureOdds.value_label,
                ],
                set_={
                    "odd": stmt.excluded.odd,
                    "api_update": stmt.excluded.api_update,
                },
            )
            await session.execute(stmt)
            stored += len(values)
    return {"count": stored, "skipped": skipped}


async def replace_odds_mapping(
    session: AsyncSession,
    items: Sequence[OddsMappingItem],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    synced = now or datetime.now(UTC)
    fixture_ids = [item.fixture.id for item in items]
    existing: set[int] = set()
    if fixture_ids:
        existing = set(
            await session.scalars(select(Fixture.id).where(Fixture.id.in_(fixture_ids)))
        )
    league_ids = {
        item.league.id
        for item in items
        if item.league is not None and item.league.id is not None
    }
    known_leagues: set[int] = set()
    if league_ids:
        known_leagues = set(
            await session.scalars(select(League.id).where(League.id.in_(league_ids)))
        )
    await session.execute(delete(OddsFixtureMapping))
    rows: dict[int, dict[str, Any]] = {}
    skipped = 0
    for item in items:
        fixture_id = item.fixture.id
        if fixture_id not in existing:
            skipped += 1
            continue
        league_id = item.league.id if item.league is not None else None
        if league_id is not None and league_id not in known_leagues:
            league_id = None
        season = item.league.season if item.league is not None else None
        rows[fixture_id] = {
            "fixture_id": fixture_id,
            "league_id": league_id,
            "season": season,
            "synced_at": synced,
        }
    stored = 0
    if rows:
        for chunk in _chunks(list(rows.values())):
            stmt = insert(OddsFixtureMapping).values(list(chunk))
            await session.execute(stmt)
            stored += len(chunk)
    return {
        "count": stored,
        "skipped": skipped,
        "fixture_ids": sorted(rows),
    }
