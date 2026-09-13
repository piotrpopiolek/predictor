from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.football import FootballClient
from predictor.constants import IRREGULAR_FIXTURE_STATUSES
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture, LeagueRound, Team, Venue
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings, load_settings
from predictor.services.ingest.fixtures import FixtureIngest
from predictor.services.queue import ensure_cursors, get_cursor_task


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _envelope(
    response: Any,
    *,
    current: int = 1,
    total: int = 1,
) -> dict[str, Any]:
    return {
        "errors": [],
        "results": len(response) if isinstance(response, list) else 1,
        "paging": {"current": current, "total": total},
        "response": response,
    }


def _fixture_payload(
    *,
    fixture_id: int,
    status: str = "NS",
    league_id: int = 39,
    home_id: int = 33,
    away_id: int = 34,
    venue_id: int | None = 556,
    extra_field: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "fixture": {
            "id": fixture_id,
            "referee": "M. Oliver",
            "timezone": "UTC",
            "date": "2026-09-13T15:00:00+00:00",
            "timestamp": 1789311600,
            "periods": {"first": None, "second": None},
            "venue": {
                "id": venue_id,
                "name": "Stadium",
                "city": "London",
            },
            "status": {
                "long": "Not Started",
                "short": status,
                "elapsed": None,
                "extra": None,
            },
        },
        "league": {
            "id": league_id,
            "name": "Premier League",
            "country": "England",
            "logo": None,
            "flag": None,
            "season": 2026,
            "round": "Regular Season - 4",
        },
        "teams": {
            "home": {
                "id": home_id,
                "name": "Home FC",
                "logo": None,
                "winner": None,
            },
            "away": {
                "id": away_id,
                "name": "Away FC",
                "logo": None,
                "winner": None,
            },
        },
        "goals": {"home": None, "away": None},
        "score": {
            "halftime": {"home": None, "away": None},
            "fulltime": {"home": None, "away": None},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }
    if extra_field:
        body["mystery"] = True
    if status == "PST":
        body["fixture"]["status"]["long"] = "Match Postponed"
    if status == "CANC":
        body["fixture"]["status"]["long"] = "Match Cancelled"
    return body


def _router(paths: list[str]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.url.path}?{request.url.query.decode()}")
        path = request.url.path
        page = request.url.params.get("page")
        day = request.url.params.get("date")
        if path == "/fixtures" and day == "2026-09-13":
            if page is None:
                return httpx.Response(
                    200,
                    json=_envelope(
                        [
                            _fixture_payload(fixture_id=1001, extra_field=True),
                            _fixture_payload(
                                fixture_id=1002,
                                status="PST",
                                home_id=40,
                                away_id=41,
                                venue_id=None,
                            ),
                        ],
                        current=1,
                        total=2,
                    ),
                )
            return httpx.Response(
                200,
                json=_envelope(
                    [_fixture_payload(fixture_id=1003, status="CANC")],
                    current=2,
                    total=2,
                ),
            )
        if path == "/fixtures" and day == "2026-09-12":
            return httpx.Response(
                200,
                json=_envelope(
                    [
                        _fixture_payload(
                            fixture_id=2001,
                            home_id=50,
                            away_id=51,
                            venue_id=600,
                        )
                    ]
                ),
            )
        if path == "/fixtures" and day == "2026-09-11":
            return httpx.Response(200, json=_envelope([]))
        if path == "/fixtures/rounds":
            return httpx.Response(
                200,
                json=_envelope(
                    [
                        {
                            "round": "Regular Season - 4",
                            "dates": ["2026-09-13"],
                        }
                    ]
                ),
            )
        raise AssertionError(f"unexpected {request.url}")

    return handler


def _client(settings: Settings, transport: httpx.BaseTransport) -> FootballClient:
    return FootballClient(
        settings,
        locked=True,
        transport=transport,
        retry_wait=WaitZero(),
    )


def test_irregular_statuses_match_fr014() -> None:
    assert IRREGULAR_FIXTURE_STATUSES == {"PST", "CANC", "ABD", "AWD", "WO"}


@pytest.mark.asyncio
async def test_forward_upserts_fixtures_pages_and_children() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        async with factory() as session:
            async with session.begin():
                await ensure_cursors(session)
        ingest = FixtureIngest(client, factory, now_fn=lambda: NOW)
        await ingest.refresh_forward()
        first_fixture_gets = [p for p in paths if p.startswith("/fixtures?")]
        await ingest.refresh_forward()

        async with factory() as session:
            stored = list(
                await session.scalars(select(Fixture.id).order_by(Fixture.id))
            )
            pst = await session.get(Fixture, 1002)
            canc = await session.get(Fixture, 1003)
            team = await session.get(Team, 33)
            venue = await session.get(Venue, 556)
            day_task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/fixtures")
                .where(EtlTask.day_utc == NOW.date())
                .where(EtlTask.fixture_id.is_(None))
            )
            child = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/fixtures")
                .where(EtlTask.fixture_id == 1001)
            )
            round_row = await session.get(LeagueRound, (39, 2026, "Regular Season - 4"))
            n_fixtures = await session.scalar(select(func.count()).select_from(Fixture))

        assert stored == [1001, 1002, 1003]
        assert pst is not None and pst.status_short == "PST"
        assert canc is not None and canc.status_short == "CANC"
        assert team is not None
        assert venue is not None
        assert day_task is not None
        assert day_task.status == "complete"
        assert day_task.paging_current == 2
        assert day_task.paging_total == 2
        assert child is not None
        assert child.status == "pending"
        assert child.params.get("id") == 1001
        assert round_row is not None
        assert n_fixtures == 3
        assert any("date=2026-09-13" in p and "page=2" in p for p in first_fixture_gets)
        dated = [
            p for p in paths if "date=2026-09-13" in p and p.startswith("/fixtures?")
        ]
        assert len(dated) >= 4
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_waits_for_today_then_moves_yesterday() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        async with factory() as session:
            async with session.begin():
                await ensure_cursors(session)
        ingest = FixtureIngest(client, factory, now_fn=lambda: NOW)
        await ingest.refresh_backfill()
        assert not any("date=2026-09-12" in p for p in paths)

        await ingest.refresh_forward()
        await ingest.refresh_backfill()

        async with factory() as session:
            yesterday = await session.get(Fixture, 2001)
            cursor = await session.scalar(
                select(EtlTask).where(EtlTask.cursor_kind == "backfill")
            )
            empty_day = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/fixtures")
                .where(EtlTask.day_utc == NOW.date().replace(day=11))
            )

        assert yesterday is not None
        assert cursor is not None
        assert cursor.params.get("last_complete") == "2026-09-12"
        assert cursor.params.get("next_day") == "2026-09-11"
        assert empty_day is None
        assert any("date=2026-09-12" in p for p in paths)
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_day_is_complete() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    empty_now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    try:
        async with factory() as session:
            async with session.begin():
                await ensure_cursors(session)
        ingest = FixtureIngest(client, factory, now_fn=lambda: empty_now)
        await ingest.refresh_forward()
        async with factory() as session:
            task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/fixtures")
                .where(EtlTask.day_utc == empty_now.date())
                .where(EtlTask.fixture_id.is_(None))
            )
        assert task is not None
        assert task.status == "complete"
        assert task.params.get("count") == 0
    finally:
        await client.aclose()
        await engine.dispose()
