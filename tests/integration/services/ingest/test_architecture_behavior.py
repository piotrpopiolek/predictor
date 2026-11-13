"""Run the paths the architecture greps only name.

A second live snapshot with the same key must not replace the odd.
GET must not settle a finished bet or expire a stale coupon.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.football import FootballClient
from predictor.models.catalog import League, LeagueSeason, OddsLiveBet
from predictor.models.fixtures import Fixture, Team
from predictor.models.odds import FixtureOddsLive
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.live import OddsLiveItem
from predictor.schemas.settings import Settings, load_settings
from predictor.services.ingest.candidate_refresh import persist_targeted_live
from predictor.services.ingest.persist_live import persist_odds_live_snapshots

_LIVE = 991_711
_LIVE_LEAGUE = 991_710
_LIVE_HOME = 991_712
_LIVE_AWAY = 991_713
_LIVE_BET = 991_715
_PULL = 991_731
_PULL_LEAGUE = 991_730
_PULL_HOME = 991_732
_PULL_AWAY = 991_733
_PULL_BET = 991_735
_BOARD = 991_741
_BOARD_LEAGUE = 991_740
_BOARD_HOME = 991_742
_BOARD_AWAY = 991_743
_STRATEGY = "arch-behavior-llm"
_T0 = datetime(2026, 11, 13, 16, 0, tzinfo=UTC)

SessionFactory = async_sessionmaker[AsyncSession]


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


def _client(
    settings: Settings, handler: Callable[[httpx.Request], httpx.Response]
) -> FootballClient:
    return FootballClient(
        settings,
        locked=True,
        transport=httpx.MockTransport(handler),
        retry_wait=WaitZero(),
    )


def _envelope(response: Any) -> dict[str, Any]:
    return {
        "errors": [],
        "results": 1,
        "paging": {"current": 1, "total": 1},
        "response": response,
    }


def _live_item(odd: str, *, suspended: bool) -> OddsLiveItem:
    return OddsLiveItem.model_validate(
        {
            "fixture": {
                "id": _LIVE,
                "status": {"long": "First Half", "elapsed": 12, "short": "1H"},
            },
            "league": {"id": _LIVE_LEAGUE, "season": 2026},
            "teams": {
                "home": {"id": _LIVE_HOME, "goals": 0},
                "away": {"id": _LIVE_AWAY, "goals": 0},
            },
            "status": {"stopped": False, "blocked": False, "finished": False},
            "odds": [
                {
                    "id": _LIVE_BET,
                    "name": "Over/Under Line",
                    "values": [
                        {
                            "value": "Over",
                            "odd": odd,
                            "handicap": None,
                            "main": True,
                            "suspended": suspended,
                        }
                    ],
                }
            ],
        }
    )


def _fixture_body(
    fixture_id: int, league_id: int, home: int, away: int
) -> dict[str, Any]:
    return {
        "fixture": {
            "id": fixture_id,
            "status": {"long": "First Half", "short": "1H", "elapsed": 12},
        },
        "league": {"id": league_id, "name": "arch-live", "season": 2026},
        "teams": {
            "home": {"id": home, "name": "Home"},
            "away": {"id": away, "name": "Away"},
        },
        "goals": {"home": 0, "away": 0},
    }


def _odds_body(odd: str) -> dict[str, Any]:
    return {
        "fixture": {"id": _PULL, "status": {"long": "First Half", "elapsed": 12}},
        "league": {"id": _PULL_LEAGUE, "season": 2026},
        "teams": {
            "home": {"id": _PULL_HOME, "goals": 0},
            "away": {"id": _PULL_AWAY, "goals": 0},
        },
        "status": {"stopped": False, "blocked": False, "finished": False},
        "odds": [
            {
                "id": _PULL_BET,
                "name": "Over/Under Line",
                "values": [
                    {
                        "value": "Over",
                        "odd": odd,
                        "handicap": None,
                        "main": True,
                        "suspended": False,
                    }
                ],
            }
        ],
    }


@asynccontextmanager
async def _database() -> AsyncIterator[SessionFactory]:
    engine = make_async_engine(load_settings())
    factory = make_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _wipe_live(session: AsyncSession, fixture_id: int, league_id: int) -> None:
    await session.execute(
        delete(FixtureOddsLive).where(FixtureOddsLive.fixture_id == fixture_id)
    )
    await session.execute(delete(Fixture).where(Fixture.id == fixture_id))
    await session.execute(
        delete(LeagueSeason).where(LeagueSeason.league_id == league_id)
    )
    await session.execute(delete(League).where(League.id == league_id))


async def _odds(factory: SessionFactory, fixture_id: int) -> list[FixtureOddsLive]:
    async with factory() as session:
        rows = await session.scalars(
            select(FixtureOddsLive)
            .where(FixtureOddsLive.fixture_id == fixture_id)
            .order_by(FixtureOddsLive.captured_at, FixtureOddsLive.odd)
        )
    return list(rows)


@pytest.mark.asyncio
async def test_live_snapshot_keeps_the_first_odd_for_the_same_key() -> None:
    async with _database() as factory:
        try:
            async with factory() as session, session.begin():
                await _wipe_live(session, _LIVE, _LIVE_LEAGUE)
                await session.execute(
                    delete(OddsLiveBet).where(OddsLiveBet.id == _LIVE_BET)
                )
                await session.execute(
                    delete(Team).where(Team.id.in_((_LIVE_HOME, _LIVE_AWAY)))
                )
                await persist_odds_live_snapshots(
                    session,
                    [_live_item("1.50", suspended=False)],
                    captured_at=_T0,
                    mapped_next_goal=set(),
                    discovered_fixture_ids={_LIVE},
                )
            async with factory() as session, session.begin():
                await persist_odds_live_snapshots(
                    session,
                    [_live_item("9.99", suspended=True)],
                    captured_at=_T0,
                    mapped_next_goal=set(),
                    discovered_fixture_ids={_LIVE},
                )
            rows = await _odds(factory, _LIVE)
            assert len(rows) == 1
            assert rows[0].odd == Decimal("1.50")
            assert rows[0].suspended is False
            later = _T0 + timedelta(seconds=60)
            async with factory() as session, session.begin():
                await persist_odds_live_snapshots(
                    session,
                    [_live_item("2.20", suspended=False)],
                    captured_at=later,
                    mapped_next_goal=set(),
                    discovered_fixture_ids={_LIVE},
                )
            rows = await _odds(factory, _LIVE)
            assert [row.captured_at for row in rows] == [_T0, later]
            assert [row.odd for row in rows] == [Decimal("1.50"), Decimal("2.20")]
        finally:
            async with factory() as session, session.begin():
                await session.execute(
                    delete(FixtureOddsLive).where(FixtureOddsLive.fixture_id == _LIVE)
                )
                await session.execute(
                    delete(OddsLiveBet).where(OddsLiveBet.id == _LIVE_BET)
                )
                await _wipe_live(session, _LIVE, _LIVE_LEAGUE)
                await session.execute(
                    delete(Team).where(Team.id.in_((_LIVE_HOME, _LIVE_AWAY)))
                )


@pytest.mark.asyncio
async def test_candidate_refresh_does_not_replace_an_existing_snapshot() -> None:
    odds = iter(("1.50", "9.99"))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/fixtures":
            assert request.url.params.get("id") == str(_PULL)
            body = _fixture_body(_PULL, _PULL_LEAGUE, _PULL_HOME, _PULL_AWAY)
        elif path == "/odds/live":
            assert request.url.params.get("fixture") == str(_PULL)
            body = _odds_body(next(odds))
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=_envelope([body]))

    settings = load_settings()
    client = _client(settings, handler)
    async with _database() as factory:
        try:
            async with factory() as session, session.begin():
                await session.execute(
                    delete(FixtureOddsLive).where(FixtureOddsLive.fixture_id == _PULL)
                )
                await session.execute(
                    delete(OddsLiveBet).where(OddsLiveBet.id == _PULL_BET)
                )
                await _wipe_live(session, _PULL, _PULL_LEAGUE)
                await session.execute(
                    delete(Team).where(Team.id.in_((_PULL_HOME, _PULL_AWAY)))
                )
            assert (
                await persist_targeted_live(client, factory, [_PULL], now=_T0) is True
            )
            assert (
                await persist_targeted_live(client, factory, [_PULL], now=_T0) is True
            )
            rows = await _odds(factory, _PULL)
            assert len(rows) == 1
            assert rows[0].odd == Decimal("1.50")
            assert rows[0].captured_at == _T0
            assert client.calls_made == 4
        finally:
            await client.aclose()
            async with factory() as session, session.begin():
                await session.execute(
                    delete(FixtureOddsLive).where(FixtureOddsLive.fixture_id == _PULL)
                )
                await session.execute(
                    delete(OddsLiveBet).where(OddsLiveBet.id == _PULL_BET)
                )
                await _wipe_live(session, _PULL, _PULL_LEAGUE)
                await session.execute(
                    delete(Team).where(Team.id.in_((_PULL_HOME, _PULL_AWAY)))
                )
