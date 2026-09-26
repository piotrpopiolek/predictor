from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.football import FootballClient
from predictor.models.catalog import LeagueSeason, OddsBet
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.models.odds import FixtureOdds, FixtureOddsLive, OddsFixtureMapping
from predictor.models.predictions import Prediction, PredictionH2H
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.fixtures import FixtureItem
from predictor.schemas.odds import PrematchOddsItem
from predictor.schemas.settings import Settings, load_settings
from predictor.services.ingest.persist_fixtures import upsert_fixtures
from predictor.services.ingest.prematch import PrematchIngest
from predictor.services.queue import ensure_cursors, ensure_odds_task


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


def test_prematch_odds_accept_numeric_bet_labels() -> None:
    item = PrematchOddsItem.model_validate(
        {
            "fixture": {"id": 1606666},
            "bookmakers": [
                {
                    "id": 8,
                    "name": "Bet365",
                    "bets": [
                        {
                            "id": 1,
                            "name": "Match Winner",
                            "values": [{"value": "Home", "odd": "1.87"}],
                        },
                        {
                            "id": 31,
                            "name": "Exact Goals Number",
                            "values": [
                                {"value": 0, "odd": "8.00"},
                                {"value": 2.5, "odd": "3.10"},
                            ],
                        },
                    ],
                }
            ],
        }
    )
    labels = [value.value for bet in item.bookmakers[0].bets for value in bet.values]
    assert labels == ["Home", "0", "2.5"]
    assert item.bookmakers[0].bets[0].values[0].odd == "1.87"


NOW = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
FIXTURE_ID = 9101
MISSING_ID = 9199
LEAGUE_ID = 39
SEASON = 2026
W7_IDS = (FIXTURE_ID, MISSING_ID)


def _envelope(response: Any, *, current: int = 1, total: int = 1) -> dict[str, Any]:
    return {
        "errors": [],
        "results": len(response) if isinstance(response, list) else 1,
        "paging": {"current": current, "total": total},
        "response": response,
    }


def _fixture_core(fixture_id: int = FIXTURE_ID) -> dict[str, Any]:
    return {
        "fixture": {
            "id": fixture_id,
            "timezone": "UTC",
            "date": "2026-09-13T15:00:00+00:00",
            "timestamp": 1789311600,
            "periods": {"first": None, "second": None},
            "venue": {"id": 556, "name": "Stadium", "city": "London"},
            "status": {
                "long": "Not Started",
                "short": "NS",
                "elapsed": None,
                "extra": None,
            },
        },
        "league": {
            "id": LEAGUE_ID,
            "name": "Premier League",
            "country": "England",
            "logo": None,
            "flag": None,
            "season": SEASON,
            "round": "Regular Season - 4",
        },
        "teams": {
            "home": {"id": 33, "name": "Home FC", "logo": None, "winner": None},
            "away": {"id": 34, "name": "Away FC", "logo": None, "winner": None},
        },
        "goals": {"home": None, "away": None},
        "score": {
            "halftime": {"home": None, "away": None},
            "fulltime": {"home": None, "away": None},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }


def _odds_item(
    *, bookmaker_id: int = 8, values: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    if values is None:
        values = [
            {"value": "Home", "odd": "1.73"},
            {"value": "Draw", "odd": "3.50"},
            {"value": "Away", "odd": "5.00"},
        ]
    return {
        "league": {"id": LEAGUE_ID, "name": "Premier League", "season": SEASON},
        "fixture": {
            "id": FIXTURE_ID,
            "timezone": "UTC",
            "date": "2026-09-13T15:00:00+00:00",
            "timestamp": 1789311600,
        },
        "update": "2026-09-13T12:00:00+00:00",
        "bookmakers": [
            {
                "id": bookmaker_id,
                "name": "Bet365",
                "bets": [
                    {
                        "id": 1,
                        "name": "Match Winner",
                        "values": values,
                    }
                ],
            }
        ],
    }


def _mapping_item(fixture_id: int) -> dict[str, Any]:
    return {
        "league": {"id": LEAGUE_ID, "season": SEASON},
        "fixture": {
            "id": fixture_id,
            "timezone": "UTC",
            "date": "2026-09-13T15:00:00+00:00",
            "timestamp": 1789311600,
        },
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
    mapping: list[dict[str, Any]] | None = None,
    odds_pages: list[list[dict[str, Any]]] | None = None,
    empty_odds: bool = False,
) -> Any:
    mapped = mapping if mapping is not None else [_mapping_item(FIXTURE_ID)]
    pages = odds_pages or [[_odds_item()]]

    def handler(request: httpx.Request) -> httpx.Response:
        query = str(request.url.params)
        paths.append(f"{request.url.path}?{query}" if query else request.url.path)
        path = request.url.path
        params = request.url.params
        if "ids" in params:
            raise AssertionError(f"ids= is Free-blocked: {request.url}")
        if path == "/odds/mapping":
            return httpx.Response(200, json=_envelope(mapped))
        if path == "/odds":
            assert params.get("fixture") == str(FIXTURE_ID)
            if empty_odds:
                return httpx.Response(200, json=_envelope([]))
            page = int(params.get("page") or "1")
            total = len(pages)
            index = page - 1
            body = pages[index] if 0 <= index < total else []
            return httpx.Response(200, json=_envelope(body, current=page, total=total))
        raise AssertionError(f"unexpected {request.url}")

    return handler


async def _reset_w7(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        async with session.begin():
            await session.execute(
                delete(EtlTask)
                .where(
                    EtlTask.endpoint.in_(
                        (
                            "/fixtures/headtohead",
                            "/predictions",
                            "/odds",
                            "/odds/mapping",
                        )
                    )
                )
                .where(EtlTask.cursor_kind.is_(None))
            )
            await session.execute(
                delete(PredictionH2H).where(
                    or_(
                        PredictionH2H.fixture_id.in_(W7_IDS),
                        PredictionH2H.h2h_fixture_id.in_(W7_IDS),
                    )
                )
            )
            await session.execute(
                delete(Prediction).where(Prediction.fixture_id.in_(W7_IDS))
            )
            await session.execute(
                delete(FixtureOdds).where(FixtureOdds.fixture_id.in_(W7_IDS))
            )
            await session.execute(
                delete(OddsFixtureMapping).where(
                    OddsFixtureMapping.fixture_id.in_(W7_IDS)
                )
            )
            await session.execute(delete(Fixture).where(Fixture.id.in_(W7_IDS)))
            await ensure_cursors(session)


async def _seed_fixture(factory: async_sessionmaker[AsyncSession]) -> None:
    item = FixtureItem.model_validate(_fixture_core())
    async with factory() as session:
        async with session.begin():
            await upsert_fixtures(session, [item])
            row = await session.get(LeagueSeason, (LEAGUE_ID, SEASON))
            if row is not None:
                row.cov_predictions = True
                row.cov_odds = True


async def _set_coverage(
    factory: async_sessionmaker[AsyncSession], **flags: bool | None
) -> None:
    async with factory() as session:
        async with session.begin():
            row = await session.get(LeagueSeason, (LEAGUE_ID, SEASON))
            assert row is not None
            for name, value in flags.items():
                setattr(row, name, value)


@pytest.mark.asyncio
async def test_mapping_snapshot_enqueues_odds_into_odds_bets() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w7(factory)
        await _seed_fixture(factory)
        ingest = PrematchIngest(client, factory, now_fn=lambda: NOW, per_tick=4)
        await ingest.refresh_pending()

        async with factory() as session:
            mapping = await session.get(OddsFixtureMapping, FIXTURE_ID)
            rows = list(
                await session.scalars(
                    select(FixtureOdds).where(FixtureOdds.fixture_id == FIXTURE_ID)
                )
            )
            bet = await session.get(OddsBet, 1)
            live_rows = await session.scalar(
                select(FixtureOddsLive).where(FixtureOddsLive.fixture_id == FIXTURE_ID)
            )
            odds_task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/odds")
                .where(EtlTask.fixture_id == FIXTURE_ID)
            )

        assert any(p.startswith("/odds/mapping") for p in paths)
        assert any(p.startswith("/odds?") and "fixture=9101" in p for p in paths)
        assert mapping is not None
        assert mapping.league_id == LEAGUE_ID
        assert mapping.season == SEASON
        assert bet is not None
        assert live_rows is None
        labels = {row.value_label for row in rows}
        assert labels == {"Home", "Draw", "Away"}
        assert all(row.bet_id == 1 for row in rows)
        assert all(row.bookmaker_id == 8 for row in rows)
        assert all(row.total is None and row.handicap is None for row in rows)
        assert odds_task is not None and odds_task.status == "complete"
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_odds_pagination_omits_page_on_first_request() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    pages = [
        [_odds_item(values=[{"value": "Home", "odd": "1.73"}])],
        [_odds_item(bookmaker_id=6, values=[{"value": "Home", "odd": "1.80"}])],
    ]
    client = _client(
        settings, httpx.MockTransport(_router(paths, mapping=[], odds_pages=pages))
    )
    try:
        await _reset_w7(factory)
        await _seed_fixture(factory)
        async with factory() as session:
            async with session.begin():
                await ensure_odds_task(session, FIXTURE_ID)
        ingest = PrematchIngest(client, factory, now_fn=lambda: NOW, per_tick=4)
        await ingest.refresh_pending()

        odds_calls = [p for p in paths if p.startswith("/odds?")]
        assert odds_calls
        assert "page=" not in odds_calls[0]
        assert any("page=2" in p for p in odds_calls)

        async with factory() as session:
            rows = list(
                await session.scalars(
                    select(FixtureOdds).where(FixtureOdds.fixture_id == FIXTURE_ID)
                )
            )
        bookmakers = {row.bookmaker_id for row in rows}
        assert bookmakers == {8, 6}
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_mapping_fixture_is_skipped() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(
        settings,
        httpx.MockTransport(_router(paths, mapping=[_mapping_item(MISSING_ID)])),
    )
    try:
        await _reset_w7(factory)
        await _seed_fixture(factory)
        ingest = PrematchIngest(client, factory, now_fn=lambda: NOW, per_tick=4)
        await ingest.refresh_pending()

        async with factory() as session:
            mapping = await session.get(OddsFixtureMapping, MISSING_ID)
            odds_task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/odds")
                .where(EtlTask.fixture_id == MISSING_ID)
            )

        assert mapping is None
        assert odds_task is None
        assert not any("fixture=9199" in p for p in paths)
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_odds_coverage_false_skips_http() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths, mapping=[])))
    try:
        await _reset_w7(factory)
        await _seed_fixture(factory)
        await _set_coverage(factory, cov_odds=False)
        async with factory() as session:
            async with session.begin():
                await ensure_odds_task(session, FIXTURE_ID)
        ingest = PrematchIngest(client, factory, now_fn=lambda: NOW, per_tick=4)
        await ingest.refresh_pending()

        async with factory() as session:
            task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/odds")
                .where(EtlTask.fixture_id == FIXTURE_ID)
            )

        assert not any(p.startswith("/odds?") for p in paths)
        assert task is not None
        assert task.status == "coverage_empty"
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_odds_are_coverage_empty() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(
        settings, httpx.MockTransport(_router(paths, mapping=[], empty_odds=True))
    )
    try:
        await _reset_w7(factory)
        await _seed_fixture(factory)
        async with factory() as session:
            async with session.begin():
                await ensure_odds_task(session, FIXTURE_ID)
        ingest = PrematchIngest(client, factory, now_fn=lambda: NOW, per_tick=4)
        await ingest.refresh_pending()

        async with factory() as session:
            task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/odds")
                .where(EtlTask.fixture_id == FIXTURE_ID)
            )

        assert any(p.startswith("/odds?") for p in paths)
        assert task is not None
        assert task.status == "coverage_empty"
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_complete_mapping_is_not_refetched_same_utc_day() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths, mapping=[])))
    try:
        await _reset_w7(factory)
        await _seed_fixture(factory)
        async with factory() as session:
            async with session.begin():
                session.add(
                    EtlTask(
                        endpoint="/odds/mapping",
                        params={},
                        status="complete",
                        completed_at=NOW,
                        updated_at=NOW,
                    )
                )
                await ensure_odds_task(session, FIXTURE_ID)
        ingest = PrematchIngest(client, factory, now_fn=lambda: NOW, per_tick=4)
        await ingest.refresh_pending()

        assert not any(p.startswith("/odds/mapping") for p in paths)
        assert any(p.startswith("/odds?") and "fixture=9101" in p for p in paths)
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_mapping_retryable_error_waits_for_backoff() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths, mapping=[])))
    try:
        await _reset_w7(factory)
        async with factory() as session:
            async with session.begin():
                session.add(
                    EtlTask(
                        endpoint="/odds/mapping",
                        params={},
                        status="retryable_error",
                        last_error="FootballHttpError",
                        updated_at=NOW,
                    )
                )
        early = PrematchIngest(
            client,
            factory,
            now_fn=lambda: NOW + timedelta(minutes=1),
            per_tick=4,
        )
        await early.refresh_pending()
        assert not any(p.startswith("/odds/mapping") for p in paths)

        due = PrematchIngest(
            client,
            factory,
            now_fn=lambda: NOW + timedelta(minutes=15),
            per_tick=4,
        )
        await due.refresh_pending()
        assert any(p.startswith("/odds/mapping") for p in paths)
    finally:
        await client.aclose()
        await engine.dispose()
