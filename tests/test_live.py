from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.football import FootballClient
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.models.odds import FixtureOddsLive
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings, load_settings
from predictor.services.ingest.live import LiveIngest
from predictor.services.queue import get_or_create_endpoint_task


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


START = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)


def _envelope(response: Any, *, current: int = 1, total: int = 1) -> dict[str, Any]:
    return {
        "errors": [],
        "results": len(response) if isinstance(response, list) else 1,
        "paging": {"current": current, "total": total},
        "response": response,
    }


def _live_fixture_payload(fixture_id: int = 9001) -> dict[str, Any]:
    return {
        "fixture": {
            "id": fixture_id,
            "timezone": "UTC",
            "date": "2026-09-13T15:00:00+00:00",
            "timestamp": 1789311600,
            "periods": {"first": 1789311600, "second": None},
            "venue": {"id": 556, "name": "Stadium", "city": "London"},
            "status": {
                "long": "First Half",
                "short": "1H",
                "elapsed": 12,
                "extra": None,
            },
        },
        "league": {
            "id": 39,
            "name": "Premier League",
            "country": "England",
            "logo": None,
            "flag": None,
            "season": 2026,
            "round": "Regular Season - 4",
        },
        "teams": {
            "home": {"id": 33, "name": "Home", "logo": None, "winner": None},
            "away": {"id": 34, "name": "Away", "logo": None, "winner": None},
        },
        "goals": {"home": 0, "away": 0},
        "score": {
            "halftime": {"home": None, "away": None},
            "fulltime": {"home": None, "away": None},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }


def _odds_live_payload(
    *,
    fixture_id: int = 9001,
    bets: list[dict[str, Any]] | None = None,
    blocked: bool = False,
    stopped: bool = False,
) -> dict[str, Any]:
    if bets is None:
        bets = [
            {
                "id": 73,
                "name": "Which team will score the 1st goal?",
                "values": [
                    {
                        "value": "Home",
                        "odd": "1.90",
                        "handicap": None,
                        "main": True,
                        "suspended": False,
                    },
                    {
                        "value": "Away",
                        "odd": "3.40",
                        "handicap": None,
                        "main": False,
                        "suspended": False,
                    },
                ],
            },
            {
                "id": 93,
                "name": "Match Corners",
                "values": [
                    {
                        "value": "Over",
                        "odd": "1.90",
                        "handicap": "10",
                        "main": True,
                        "suspended": False,
                    }
                ],
            },
        ]
    return {
        "fixture": {
            "id": fixture_id,
            "status": {"long": "First Half", "elapsed": 12, "seconds": "12:04"},
        },
        "league": {"id": 39, "season": 2026},
        "teams": {
            "home": {"id": 33, "goals": 0},
            "away": {"id": 34, "goals": 0},
        },
        "status": {"stopped": stopped, "blocked": blocked, "finished": False},
        "update": "2026-09-13T15:12:00+00:00",
        "odds": bets,
    }


def _client(settings: Settings, transport: httpx.BaseTransport) -> FootballClient:
    return FootballClient(
        settings,
        locked=True,
        transport=transport,
        retry_wait=WaitZero(),
    )


def _router(
    paths: list[str],
    *,
    odds_body: dict[str, Any] | None = None,
) -> Any:
    payload = odds_body or _odds_live_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = str(request.url.params)
        paths.append(f"{path}?{query}" if query else path)
        if path == "/fixtures" and request.url.params.get("live") == "all":
            return httpx.Response(200, json=_envelope([_live_fixture_payload()]))
        if path == "/odds/live":
            return httpx.Response(200, json=_envelope([payload]))
        raise AssertionError(f"unexpected path {path}?{query}")

    return handler


async def _reset_w5(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        async with session.begin():
            await session.execute(delete(FixtureOddsLive))
            await session.execute(
                delete(EtlTask)
                .where(EtlTask.endpoint.in_(("/odds/live", "/fixtures")))
                .where(EtlTask.cursor_kind.is_(None))
                .where(EtlTask.fixture_id.is_(None))
                .where(EtlTask.day_utc.is_(None))
            )
            await session.execute(delete(Fixture).where(Fixture.id == 9001))
            task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/odds/live/bets")
                .where(EtlTask.cursor_kind.is_(None))
                .order_by(EtlTask.id)
                .limit(1)
            )
            mapping = {
                "next_goal_bet_ids": [73, 85],
                "next_goal_names": ["Which team will score the 1st goal?"],
            }
            if task is None:
                session.add(
                    EtlTask(
                        endpoint="/odds/live/bets",
                        params=mapping,
                        status="complete",
                    )
                )
            else:
                params = dict(task.params)
                params.update(mapping)
                task.params = params


@pytest.mark.asyncio
async def test_live_all_and_odds_live_append_only() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    clock = {"t": START}

    def now() -> datetime:
        return clock["t"]

    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w5(factory)
        ingest = LiveIngest(client, factory, now_fn=now)
        await ingest.refresh_live_fixtures()
        await ingest.refresh_next_goal_snapshots()
        clock["t"] = START + timedelta(seconds=60)
        await ingest.refresh_next_goal_snapshots()

        async with factory() as session:
            n = await session.scalar(select(func.count()).select_from(FixtureOddsLive))
            fixture = await session.get(Fixture, 9001)
            first_captured = list(
                await session.scalars(select(FixtureOddsLive.captured_at).distinct())
            )
            live_task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/odds/live")
            )
            child = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/fixtures")
                .where(EtlTask.fixture_id == 9001)
            )
        assert fixture is not None
        assert fixture.status_short == "1H"
        assert n == 6
        assert len(first_captured) == 2
        assert live_task is not None
        assert live_task.status == "complete"
        assert live_task.params.get("missing_next_goal") == []
        assert child is not None
        assert child.status == "pending"
        assert any("live=all" in p for p in paths)
        assert any(p == "/odds/live" or p.startswith("/odds/live?") for p in paths)
        assert not any("/odds/bets" == p.split("?")[0] for p in paths)
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_missing_next_goal_is_observable_without_synthetic_odd() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    odds = _odds_live_payload(
        bets=[
            {
                "id": 93,
                "name": "Match Corners",
                "values": [
                    {
                        "value": "Over",
                        "odd": "1.90",
                        "handicap": "10",
                        "main": True,
                        "suspended": False,
                    }
                ],
            }
        ]
    )
    client = _client(settings, httpx.MockTransport(_router(paths, odds_body=odds)))
    try:
        await _reset_w5(factory)
        ingest = LiveIngest(client, factory, now_fn=lambda: START)
        await ingest.refresh_live_fixtures()
        await ingest.refresh_next_goal_snapshots()
        async with factory() as session:
            next_goal_rows = list(
                await session.scalars(
                    select(FixtureOddsLive).where(FixtureOddsLive.bet_id == 73)
                )
            )
            corners = list(
                await session.scalars(
                    select(FixtureOddsLive).where(FixtureOddsLive.bet_id == 93)
                )
            )
            task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/odds/live")
            )
        assert next_goal_rows == []
        assert len(corners) == 1
        assert task is not None
        assert 9001 in task.params.get("missing_next_goal", [])
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_suspended_and_blocked_flags_are_stored() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    odds = _odds_live_payload(
        blocked=True,
        bets=[
            {
                "id": 73,
                "name": "Which team will score the 1st goal?",
                "values": [
                    {
                        "value": "Home",
                        "odd": "1.90",
                        "handicap": None,
                        "main": True,
                        "suspended": True,
                    }
                ],
            }
        ],
    )
    client = _client(settings, httpx.MockTransport(_router([], odds_body=odds)))
    try:
        await _reset_w5(factory)
        ingest = LiveIngest(client, factory, now_fn=lambda: START)
        await ingest.refresh_live_fixtures()
        await ingest.refresh_next_goal_snapshots()
        async with factory() as session:
            row = await session.scalar(select(FixtureOddsLive))
        assert row is not None
        assert row.suspended is True
        assert row.blocked is True
        assert row.elapsed_seconds == "12:04"
        assert row.api_update is not None
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_live_gap_is_logged_not_filled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    client = _client(settings, httpx.MockTransport(_router([])))
    try:
        await _reset_w5(factory)
        async with factory() as session:
            async with session.begin():
                task = await get_or_create_endpoint_task(session, "/odds/live")
                task.params = {
                    "last_poll_at": (START - timedelta(minutes=10)).isoformat()
                }
        ingest = LiveIngest(client, factory, now_fn=lambda: START, target_seconds=60)
        with caplog.at_level("WARNING"):
            await ingest.refresh_live_fixtures()
            await ingest.refresh_next_goal_snapshots()
        assert "live_gap" in caplog.text
        async with factory() as session:
            n = await session.scalar(select(func.count()).select_from(FixtureOddsLive))
        assert n == 3
    finally:
        await client.aclose()
        await engine.dispose()
