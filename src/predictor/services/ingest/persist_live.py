"""Append-only persist for /odds/live snapshots (FR-016, FR-018, FR-020)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.models.catalog import OddsLiveBet
from predictor.models.odds import FixtureOddsLive
from predictor.schemas.catalog import NamedIdItem
from predictor.schemas.fixtures import (
    FixtureCore,
    FixtureGoals,
    FixtureItem,
    FixtureLeague,
    FixtureStatus,
    FixtureTeam,
    FixtureTeams,
)
from predictor.schemas.live import LiveOddBet, OddsLiveItem
from predictor.services.ingest.next_goal import is_next_goal_market
from predictor.services.ingest.persist import _chunks, upsert_named_ids
from predictor.services.ingest.persist_fixtures import upsert_fixtures

_VALUE_LABEL_MAX = 64
_HANDICAP_MAX = 16


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


def _handicap_key(raw: object) -> str | None:
    if raw is None:
        return ""
    text = str(raw).strip()
    if len(text) > _HANDICAP_MAX:
        return None
    return text


def odds_item_as_fixture(item: OddsLiveItem) -> FixtureItem | None:
    if item.league is None or item.league.season is None:
        return None
    if item.teams is None or item.teams.home is None or item.teams.away is None:
        return None
    home_id = item.teams.home.id
    away_id = item.teams.away.id
    if home_id is None or away_id is None:
        return None
    stub_status = item.fixture.status
    return FixtureItem(
        fixture=FixtureCore(
            id=item.fixture.id,
            status=FixtureStatus(
                long=stub_status.long if stub_status is not None else None,
                elapsed=stub_status.elapsed if stub_status is not None else None,
            ),
        ),
        league=FixtureLeague(id=item.league.id, season=item.league.season),
        teams=FixtureTeams(
            home=FixtureTeam(id=home_id),
            away=FixtureTeam(id=away_id),
        ),
        goals=FixtureGoals(
            home=item.teams.home.goals,
            away=item.teams.away.goals,
        ),
    )


def next_goal_ids_from_mapping(raw: object) -> set[int]:
    if not isinstance(raw, list):
        return set()
    found: set[int] = set()
    for item in raw:
        try:
            found.add(int(item))
        except (TypeError, ValueError):
            continue
    return found


def next_goal_bet_ids_in_item(item: OddsLiveItem, mapped: set[int]) -> set[int]:
    present: set[int] = set()
    for bet in item.odds:
        if bet.id in mapped or (bet.name is not None and is_next_goal_market(bet.name)):
            present.add(bet.id)
    return present


async def ensure_live_fixtures(
    session: AsyncSession, items: list[OddsLiveItem]
) -> set[int]:
    parsed: list[FixtureItem] = []
    for item in items:
        fixture = odds_item_as_fixture(item)
        if fixture is not None:
            parsed.append(fixture)
    if not parsed:
        return set()
    extra = await upsert_fixtures(session, parsed)
    return {int(fid) for fid in extra.get("fixture_ids", [])}


async def persist_odds_live_snapshots(
    session: AsyncSession,
    items: list[OddsLiveItem],
    *,
    captured_at: datetime,
    mapped_next_goal: set[int],
    discovered_fixture_ids: set[int],
) -> dict[str, Any]:
    stored_ids = await ensure_live_fixtures(session, items)
    bets: list[NamedIdItem] = []
    for item in items:
        for bet in item.odds:
            bets.append(NamedIdItem(id=bet.id, name=bet.name))
    await upsert_named_ids(session, OddsLiveBet, bets)

    rows: list[dict[str, Any]] = []
    missing: list[int] = []
    blocked_empty: list[int] = []
    seen_fixtures: set[int] = set()
    for item in items:
        fixture_id = item.fixture.id
        seen_fixtures.add(fixture_id)
        if fixture_id not in stored_ids and fixture_id not in discovered_fixture_ids:
            continue
        next_ids = next_goal_bet_ids_in_item(item, mapped_next_goal)
        expected = mapped_next_goal or next_ids
        if expected and not next_ids:
            missing.append(fixture_id)
        flags = item.status
        stopped = flags.stopped if flags is not None else None
        blocked = flags.blocked if flags is not None else None
        finished = flags.finished if flags is not None else None
        if blocked and not item.odds:
            blocked_empty.append(fixture_id)
        stub = item.fixture.status
        home_goals = item.teams.home.goals if item.teams and item.teams.home else None
        away_goals = item.teams.away.goals if item.teams and item.teams.away else None
        for bet in item.odds:
            rows.extend(
                _value_rows(
                    item,
                    bet,
                    captured_at=captured_at,
                    stopped=stopped,
                    blocked=blocked,
                    finished=finished,
                    stub=stub,
                    home_goals=home_goals,
                    away_goals=away_goals,
                )
            )
    for fixture_id in sorted(discovered_fixture_ids - seen_fixtures):
        missing.append(fixture_id)

    inserted = 0
    if rows:
        for chunk in _chunks(rows):
            stmt = insert(FixtureOddsLive).values(list(chunk))
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[
                    FixtureOddsLive.fixture_id,
                    FixtureOddsLive.bet_id,
                    FixtureOddsLive.value_label,
                    FixtureOddsLive.handicap,
                    FixtureOddsLive.captured_at,
                ]
            )
            result = await session.execute(stmt)
            inserted += result.rowcount or 0

    return {
        "count": inserted if rows else 0,
        "fixtures": len(seen_fixtures),
        "missing_next_goal": sorted(set(missing)),
        "blocked_without_odds": sorted(set(blocked_empty)),
        "stored_fixture_ids": sorted(stored_ids),
    }


def _value_rows(
    item: OddsLiveItem,
    bet: LiveOddBet,
    *,
    captured_at: datetime,
    stopped: bool | None,
    blocked: bool | None,
    finished: bool | None,
    stub: Any,
    home_goals: int | None,
    away_goals: int | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    status_long = stub.long if stub is not None else None
    elapsed_minutes = stub.elapsed if stub is not None else None
    elapsed_seconds = stub.seconds if stub is not None else None
    for value in bet.values:
        label = (value.value or "").strip()
        if not label or len(label) > _VALUE_LABEL_MAX:
            continue
        handicap = _handicap_key(value.handicap)
        if handicap is None:
            continue
        odd = _parse_odd(value.odd)
        if odd is None:
            continue
        rows.append(
            {
                "fixture_id": item.fixture.id,
                "bet_id": bet.id,
                "value_label": label,
                "handicap": handicap,
                "odd": odd,
                "is_main": value.main,
                "suspended": value.suspended,
                "stopped": stopped,
                "blocked": blocked,
                "finished": finished,
                "status_long": status_long,
                "elapsed_minutes": elapsed_minutes,
                "elapsed_seconds": elapsed_seconds,
                "league_id": item.league.id if item.league is not None else None,
                "season": item.league.season if item.league is not None else None,
                "home_goals": home_goals,
                "away_goals": away_goals,
                "api_update": item.update,
                "captured_at": captured_at,
            }
        )
    return rows
