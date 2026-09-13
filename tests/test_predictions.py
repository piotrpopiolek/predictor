from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.football import FootballClient
from predictor.models.catalog import LeagueSeason
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.models.odds import FixtureOdds, OddsFixtureMapping
from predictor.models.predictions import Prediction, PredictionH2H
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.fixtures import FixtureItem
from predictor.schemas.settings import Settings, load_settings
from predictor.services.ingest.persist_fixtures import upsert_fixtures
from predictor.services.ingest.prematch import PrematchIngest
from predictor.services.queue import (
    ensure_cursors,
    ensure_h2h_task,
    ensure_predictions_task,
)


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


NOW = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
SUBJECT_ID = 9101
H2H_ID = 9102
LEAGUE_ID = 39
SEASON = 2026
W7_IDS = (SUBJECT_ID, H2H_ID)


def _envelope(response: Any, *, current: int = 1, total: int = 1) -> dict[str, Any]:
    return {
        "errors": [],
        "results": len(response) if isinstance(response, list) else 1,
        "paging": {"current": current, "total": total},
        "response": response,
    }


def _fixture_core(
    fixture_id: int,
    *,
    status: str,
    home_id: int = 33,
    away_id: int = 34,
) -> dict[str, Any]:
    long_status = {
        "FT": "Match Finished",
        "NS": "Not Started",
    }.get(status, status)
    return {
        "fixture": {
            "id": fixture_id,
            "referee": None,
            "timezone": "UTC",
            "date": "2026-08-01T15:00:00+00:00",
            "timestamp": 1785596400,
            "periods": {"first": None, "second": None},
            "venue": {"id": 556, "name": "Stadium", "city": "London"},
            "status": {
                "long": long_status,
                "short": status,
                "elapsed": 90 if status == "FT" else None,
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
            "round": "Regular Season - 1",
        },
        "teams": {
            "home": {"id": home_id, "name": "Home FC", "logo": None, "winner": None},
            "away": {"id": away_id, "name": "Away FC", "logo": None, "winner": None},
        },
        "goals": {"home": 1 if status == "FT" else None, "away": 0},
        "score": {
            "halftime": {"home": None, "away": None},
            "fulltime": {"home": 1 if status == "FT" else None, "away": 0},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }


def _prediction_payload(*, with_h2h: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {
        "predictions": {
            "winner": {"id": 33, "name": "Home FC", "comment": "Win or draw"},
            "win_or_draw": True,
            "under_over": "-2.5",
            "goals": {"home": "-1.5", "away": "-1.5"},
            "advice": "Home or draw and -2.5",
            "percent": {"home": "45%", "draw": "45%", "away": "10%"},
        },
        "league": {"id": LEAGUE_ID, "season": SEASON},
        "comparison": {"form": {"home": "50%", "away": "50%"}},
        "teams": {"home": {"id": 33, "name": "Home FC"}, "away": {"id": 34}},
        "h2h": [],
    }
    if with_h2h:
        body["h2h"] = [_fixture_core(H2H_ID, status="FT")]
    return body


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
    prediction: dict[str, Any] | None = None,
    empty_predictions: bool = False,
) -> Any:
    payload = prediction if prediction is not None else _prediction_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        query = str(request.url.params)
        paths.append(f"{request.url.path}?{query}" if query else request.url.path)
        path = request.url.path
        params = request.url.params
        if "ids" in params:
            raise AssertionError(f"ids= is Free-blocked: {request.url}")
        if path == "/odds/mapping":
            return httpx.Response(200, json=_envelope([]))
        if path == "/fixtures/headtohead":
            assert params.get("h2h") == "33-34"
            return httpx.Response(
                200, json=_envelope([_fixture_core(H2H_ID, status="FT")])
            )
        if path == "/predictions":
            assert params.get("fixture") == str(SUBJECT_ID)
            if empty_predictions:
                return httpx.Response(200, json=_envelope([]))
            return httpx.Response(200, json=_envelope([payload]))
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


async def _seed_subject(
    factory: async_sessionmaker[AsyncSession],
    *,
    predictions: bool = True,
    h2h: bool = True,
) -> None:
    item = FixtureItem.model_validate(
        _fixture_core(SUBJECT_ID, status="NS", home_id=33, away_id=34)
    )
    async with factory() as session:
        async with session.begin():
            await upsert_fixtures(session, [item])
            row = await session.get(LeagueSeason, (LEAGUE_ID, SEASON))
            if row is not None:
                row.cov_predictions = True
                row.cov_odds = True
            if h2h:
                await ensure_h2h_task(session, 33, 34)
            if predictions:
                await ensure_predictions_task(session, SUBJECT_ID)


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
async def test_h2h_is_stored_before_prediction_links() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w7(factory)
        await _seed_subject(factory)
        ingest = PrematchIngest(client, factory, now_fn=lambda: NOW, per_tick=4)
        await ingest.refresh_pending()

        async with factory() as session:
            past = await session.get(Fixture, H2H_ID)
            prediction = await session.get(Prediction, SUBJECT_ID)
            links = list(
                await session.scalars(
                    select(PredictionH2H).where(PredictionH2H.fixture_id == SUBJECT_ID)
                )
            )
            pred_task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/predictions")
                .where(EtlTask.fixture_id == SUBJECT_ID)
            )
            h2h_task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/fixtures/headtohead")
            )

        domain = [
            p
            for p in paths
            if p.startswith("/fixtures/headtohead") or p.startswith("/predictions")
        ]
        assert any(p.startswith("/odds/mapping") for p in paths)
        assert domain
        assert domain[0].startswith("/fixtures/headtohead")
        assert any(p.startswith("/predictions") for p in domain)
        assert past is not None
        assert prediction is not None
        assert prediction.winner_team_id == 33
        assert prediction.win_or_draw is True
        assert prediction.under_over == "-2.5"
        assert prediction.pct_home == "45%"
        assert prediction.comparison is not None
        assert len(links) == 1
        assert links[0].h2h_fixture_id == H2H_ID
        assert links[0].sort_order == 0
        assert pred_task is not None and pred_task.status == "complete"
        assert h2h_task is not None and h2h_task.status == "complete"
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_predictions_upsert_embedded_h2h_without_prior_headtohead_task() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w7(factory)
        await _seed_subject(factory, h2h=False, predictions=True)
        ingest = PrematchIngest(client, factory, now_fn=lambda: NOW, per_tick=4)
        await ingest.refresh_pending()

        async with factory() as session:
            past = await session.get(Fixture, H2H_ID)
            n_links = await session.scalar(
                select(func.count())
                .select_from(PredictionH2H)
                .where(PredictionH2H.fixture_id == SUBJECT_ID)
            )

        assert "/fixtures/headtohead" not in "".join(paths)
        assert past is not None
        assert n_links == 1
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_predictions_coverage_false_skips_http() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_w7(factory)
        await _seed_subject(factory, h2h=False)
        await _set_coverage(factory, cov_predictions=False)
        ingest = PrematchIngest(client, factory, now_fn=lambda: NOW, per_tick=4)
        await ingest.refresh_pending()

        async with factory() as session:
            task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/predictions")
                .where(EtlTask.fixture_id == SUBJECT_ID)
            )
            prediction = await session.get(Prediction, SUBJECT_ID)

        assert not any(p.startswith("/predictions") for p in paths)
        assert task is not None
        assert task.status == "coverage_empty"
        assert prediction is None
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_predictions_are_coverage_empty() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(
        settings, httpx.MockTransport(_router(paths, empty_predictions=True))
    )
    try:
        await _reset_w7(factory)
        await _seed_subject(factory, h2h=False)
        ingest = PrematchIngest(client, factory, now_fn=lambda: NOW, per_tick=4)
        await ingest.refresh_pending()

        async with factory() as session:
            task = await session.scalar(
                select(EtlTask)
                .where(EtlTask.endpoint == "/predictions")
                .where(EtlTask.fixture_id == SUBJECT_ID)
            )

        assert any(p.startswith("/predictions") for p in paths)
        assert task is not None
        assert task.status == "coverage_empty"
    finally:
        await client.aclose()
        await engine.dispose()
