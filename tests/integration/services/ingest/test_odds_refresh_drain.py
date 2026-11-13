"""Targeted odds refresh: persist the snapshot, then clear the request."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.football import FootballClient
from predictor.models.advisor import AdvisorOddsRefresh
from predictor.models.catalog import League, LeagueSeason, OddsLiveBet
from predictor.models.fixtures import Fixture, Team
from predictor.models.odds import FixtureOddsLive
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings, load_settings
from predictor.services.advisor.odds_refresh import enqueue_odds_refresh
from predictor.services.ingest import odds_refresh_drain as drain_mod
from predictor.services.ingest.odds_refresh_drain import (
    OddsRefreshWake,
    drain_odds_refresh,
)
from predictor.services.scheduler import Scheduler

_LEAGUE = 991_401
_HOME = 991_402
_AWAY = 991_403
_FIXTURE = 991_404
_BET = 991_405
_NOW = datetime(2026, 11, 13, 16, 0, tzinfo=UTC)
_QUOTA = "You have reached the request limit for the day"

SessionFactory = async_sessionmaker[AsyncSession]


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


def _envelope(response: Any, *, errors: Any = None) -> dict[str, Any]:
    return {
        "errors": [] if errors is None else errors,
        "results": len(response) if isinstance(response, list) else 1,
        "paging": {"current": 1, "total": 1},
        "response": response,
    }


def _fixture_body() -> dict[str, Any]:
    return {
        "fixture": {
            "id": _FIXTURE,
            "timezone": "UTC",
            "date": "2026-11-13T16:00:00+00:00",
            "status": {
                "long": "Second Half",
                "short": "2H",
                "elapsed": 70,
                "extra": None,
            },
        },
        "league": {"id": _LEAGUE, "name": "odds-refresh", "season": 2026},
        "teams": {
            "home": {"id": _HOME, "name": "Home", "winner": None},
            "away": {"id": _AWAY, "name": "Away", "winner": None},
        },
        "goals": {"home": 2, "away": 1},
        "score": {
            "halftime": {"home": 0, "away": 1},
            "fulltime": {"home": None, "away": None},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }


def _odds_body() -> dict[str, Any]:
    return {
        "fixture": {"id": _FIXTURE, "status": {"long": "Second Half", "elapsed": 70}},
        "league": {"id": _LEAGUE, "season": 2026},
        "teams": {"home": {"id": _HOME, "goals": 2}, "away": {"id": _AWAY, "goals": 1}},
        "status": {"stopped": False, "blocked": False, "finished": False},
        "update": "2026-11-13T16:40:00+00:00",
        "odds": [
            {
                "id": _BET,
                "name": "Over/Under Line",
                "values": [
                    {
                        "value": "Over",
                        "odd": "1.90",
                        "handicap": None,
                        "main": True,
                        "suspended": False,
                    }
                ],
            }
        ],
    }


def _client(
    settings: Settings, handler: Callable[[httpx.Request], httpx.Response]
) -> FootballClient:
    return FootballClient(
        settings,
        locked=True,
        transport=httpx.MockTransport(handler),
        retry_wait=WaitZero(),
    )


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError(request.url.path)


def _ok(paths: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        _assert_targeted(request)
        if path == "/fixtures":
            return httpx.Response(200, json=_envelope([_fixture_body()]))
        if path == "/odds/live":
            return httpx.Response(200, json=_envelope([_odds_body()]))
        raise AssertionError(path)

    return handler


def _failing(
    failure: str, where: str, paths: list[str]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        _assert_targeted(request)
        fail_here = (where == "fixtures" and path == "/fixtures") or (
            where == "odds" and path == "/odds/live"
        )
        if not fail_here:
            return httpx.Response(200, json=_envelope([_fixture_body()]))
        if failure == "auth":
            return httpx.Response(401, json={"errors": {"access": "denied"}})
        if failure == "quota":
            return httpx.Response(200, json=_envelope([], errors={"requests": _QUOTA}))
        return httpx.Response(400, json={"errors": ["bad"]})

    return handler


def _assert_targeted(request: httpx.Request) -> None:
    assert "page" not in request.url.params
    assert "live" not in request.url.params
    if request.url.path == "/fixtures":
        assert request.url.params.get("id") == str(_FIXTURE)
        return
    if request.url.path == "/odds/live":
        assert request.url.params.get("fixture") == str(_FIXTURE)
        return
    raise AssertionError(request.url.path)


async def _purge(session: AsyncSession) -> None:
    await session.execute(
        delete(AdvisorOddsRefresh).where(AdvisorOddsRefresh.fixture_id == _FIXTURE)
    )
    await session.execute(
        delete(FixtureOddsLive).where(FixtureOddsLive.fixture_id == _FIXTURE)
    )
    await session.execute(delete(OddsLiveBet).where(OddsLiveBet.id == _BET))
    await session.execute(delete(Fixture).where(Fixture.id == _FIXTURE))
    await session.execute(delete(Team).where(Team.id.in_((_HOME, _AWAY))))
    await session.execute(delete(LeagueSeason).where(LeagueSeason.league_id == _LEAGUE))
    await session.execute(delete(League).where(League.id == _LEAGUE))


def _add_fixture(session: AsyncSession) -> None:
    session.add(League(id=_LEAGUE, name="odds-refresh"))
    session.add(Team(id=_HOME, name="Home"))
    session.add(Team(id=_AWAY, name="Away"))


async def _seed(factory: SessionFactory, *, requested: bool) -> None:
    # drain_odds_refresh reads every pending id, so the queue holds only this one.
    async with factory() as session, session.begin():
        await session.execute(delete(AdvisorOddsRefresh))
        await _purge(session)
        _add_fixture(session)
        await session.flush()
        session.add(
            Fixture(
                id=_FIXTURE,
                league_id=_LEAGUE,
                season=2026,
                home_team_id=_HOME,
                away_team_id=_AWAY,
                status_short="1H",
                goals_home=0,
                goals_away=0,
            )
        )
        await session.flush()
        if requested:
            session.add(AdvisorOddsRefresh(fixture_id=_FIXTURE, requested_at=_NOW))


@asynccontextmanager
async def _database() -> AsyncIterator[SessionFactory]:
    engine = make_async_engine(load_settings())
    factory = make_session_factory(engine)
    try:
        yield factory
    finally:
        async with factory() as session, session.begin():
            await _purge(session)
        await engine.dispose()


async def _refresh_and_fixture(
    factory: SessionFactory,
) -> tuple[AdvisorOddsRefresh | None, Fixture, int]:
    async with factory() as session:
        refresh = await session.get(AdvisorOddsRefresh, _FIXTURE)
        fixture = await session.get(Fixture, _FIXTURE)
        odds = await session.scalar(
            select(func.count())
            .select_from(FixtureOddsLive)
            .where(FixtureOddsLive.fixture_id == _FIXTURE)
        )
    assert fixture is not None
    return refresh, fixture, int(odds or 0)


async def _quote(factory: SessionFactory) -> FixtureOddsLive | None:
    async with factory() as session:
        return await session.scalar(
            select(FixtureOddsLive).where(FixtureOddsLive.fixture_id == _FIXTURE)
        )


@pytest.mark.asyncio
async def test_empty_queue_does_not_call_the_api() -> None:
    paths: list[str] = []
    settings = load_settings()
    client = _client(settings, _ok(paths))
    async with _database() as factory:
        try:
            async with factory() as session, session.begin():
                await session.execute(delete(AdvisorOddsRefresh))
            cleared = await drain_odds_refresh(client, factory, now=_NOW)
        finally:
            await client.aclose()
    assert cleared == 0
    assert paths == []
    assert client.calls_made == 0


@pytest.mark.asyncio
async def test_listen_notify_wakes_the_drain() -> None:
    settings = load_settings()
    drained: list[int] = []

    async def record() -> int:
        drained.append(1)
        return 1

    client = _client(settings, _refuse)
    wake = OddsRefreshWake(settings)
    stop = asyncio.Event()
    listening = asyncio.Event()
    sleep: asyncio.Task[None] | None = None
    async with _database() as factory:
        try:
            await _seed(factory, requested=False)
            await wake.start()
            real_wait = wake.event.wait

            async def wait() -> bool:
                waiter = asyncio.create_task(real_wait())
                await asyncio.sleep(0)
                listening.set()
                return await waiter

            wake.event.wait = wait  # type: ignore[method-assign]
            scheduler = Scheduler(
                settings,
                client,
                factory,
                odds_refresh_wake=wake.event,
                drain_odds_refresh=record,
            )
            sleep = asyncio.create_task(
                scheduler._sleep_until(stop, time.monotonic() + 30)
            )
            await asyncio.wait_for(listening.wait(), timeout=2)
            async with factory() as session, session.begin():
                queued = await enqueue_odds_refresh(session, [_FIXTURE], now=_NOW)
            assert queued == (_FIXTURE,)
            await asyncio.wait_for(sleep, timeout=5)
            assert drained == [1]
            assert wake.event.is_set() is False
        finally:
            stop.set()
            if sleep is not None and not sleep.done():
                sleep.cancel()
                with suppress(asyncio.CancelledError):
                    await sleep
            await wake.close()
            await client.aclose()
    assert wake._conn is None
    assert wake._task is None


@pytest.mark.asyncio
async def test_broken_listen_is_logged_and_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = load_settings()
    wake = OddsRefreshWake(settings)
    await wake.start()
    conn = wake._conn
    task = wake._task
    assert conn is not None
    assert task is not None
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        with caplog.at_level(logging.WARNING):
            async with factory() as session, session.begin():
                await session.execute(
                    text("SELECT pg_terminate_backend(:pid)"),
                    {"pid": conn.info.backend_pid},
                )
            await asyncio.wait_for(task, timeout=5)
        assert "odds_refresh_listen_failed" in caplog.text
        assert '"service": "worker"' in caplog.text
        await wake.close()
    finally:
        if wake._conn is not None or wake._task is not None:
            await wake.close()
        await engine.dispose()
    assert wake._conn is None
    assert wake._task is None


@pytest.mark.asyncio
async def test_drain_persists_the_snapshot_then_clears_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []
    seen = {"before_clear": False}
    original = drain_mod.clear_odds_refresh

    async def spy(session: AsyncSession, fixture_ids: Any) -> None:
        stored = await session.scalar(
            select(func.count())
            .select_from(FixtureOddsLive)
            .where(FixtureOddsLive.fixture_id == _FIXTURE)
        )
        assert stored and int(stored) >= 1
        assert list(fixture_ids) == [_FIXTURE]
        seen["before_clear"] = True
        await original(session, fixture_ids)

    monkeypatch.setattr(drain_mod, "clear_odds_refresh", spy)
    settings = load_settings()
    client = _client(settings, _ok(paths))
    async with _database() as factory:
        try:
            await _seed(factory, requested=True)
            cleared = await drain_odds_refresh(client, factory, now=_NOW)
            refresh, fixture, odds = await _refresh_and_fixture(factory)
            quote = await _quote(factory)
        finally:
            await client.aclose()
    assert cleared == 1
    assert seen["before_clear"] is True
    assert paths == ["/fixtures", "/odds/live"]
    assert client.calls_made == 2
    assert refresh is None
    assert fixture.status_short == "2H"
    assert fixture.elapsed_minutes == 70
    assert fixture.goals_home == 2
    assert fixture.goals_away == 1
    assert odds == 1
    assert quote is not None
    assert quote.captured_at == _NOW
    assert quote.odd == Decimal("1.90")
    assert quote.home_goals == 2
    assert quote.away_goals == 1
    assert quote.bet_id == _BET
    assert quote.value_label == "Over"


@pytest.mark.parametrize(
    ("failure", "where"),
    [
        ("auth", "fixtures"),
        ("quota", "fixtures"),
        ("http", "fixtures"),
        ("auth", "odds"),
        ("quota", "odds"),
        ("http", "odds"),
    ],
)
@pytest.mark.asyncio
async def test_api_failure_leaves_the_refresh_request(failure: str, where: str) -> None:
    paths: list[str] = []
    settings = load_settings()
    client = _client(settings, _failing(failure, where, paths))
    async with _database() as factory:
        try:
            await _seed(factory, requested=True)
            cleared = await drain_odds_refresh(client, factory, now=_NOW)
            refresh, fixture, odds = await _refresh_and_fixture(factory)
        finally:
            await client.aclose()
    assert cleared == 0
    assert refresh is not None
    assert refresh.fixture_id == _FIXTURE
    assert fixture.status_short == "1H"
    assert fixture.goals_home == 0
    assert fixture.goals_away == 0
    assert odds == 0
    if where == "fixtures":
        assert paths == ["/fixtures"]
    else:
        assert paths == ["/fixtures", "/odds/live"]
    assert client.calls_made == len(paths)


@pytest.mark.asyncio
async def test_persist_failure_leaves_the_refresh_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(session: AsyncSession, items: object) -> None:
        del session, items
        raise RuntimeError("persist failed")

    monkeypatch.setattr(
        "predictor.services.ingest.candidate_refresh.upsert_fixtures", boom
    )
    paths: list[str] = []
    settings = load_settings()
    client = _client(settings, _ok(paths))
    async with _database() as factory:
        try:
            await _seed(factory, requested=True)
            cleared = await drain_odds_refresh(client, factory, now=_NOW)
            refresh, fixture, odds = await _refresh_and_fixture(factory)
        finally:
            await client.aclose()
    assert cleared == 0
    assert paths == ["/fixtures", "/odds/live"]
    assert refresh is not None
    assert fixture.status_short == "1H"
    assert fixture.goals_home == 0
    assert odds == 0
