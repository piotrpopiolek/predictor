from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.football import FootballClient
from predictor.constants import DICTIONARY_ENDPOINTS
from predictor.models.catalog import (
    Bookmaker,
    Country,
    League,
    LeagueSeason,
    OddsBet,
    OddsLiveBet,
    Season,
)
from predictor.models.etl import EtlTask
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.catalog import CountryItem
from predictor.schemas.settings import Settings, load_settings
from predictor.services.ingest import CatalogIngest
from predictor.services.ingest.persist import upsert_countries
from predictor.services.queue import needs_refresh


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


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


def _league_payload() -> dict[str, Any]:
    return {
        "league": {
            "id": 39,
            "name": "Premier League",
            "type": "League",
            "logo": None,
        },
        "country": {"name": "England", "code": "GB", "flag": None},
        "seasons": [
            {
                "year": 2024,
                "start": "2024-08-16",
                "end": "2025-05-25",
                "current": True,
                "coverage": {
                    "fixtures": {
                        "events": True,
                        "lineups": True,
                        "statistics_fixtures": False,
                        "statistics_players": False,
                    },
                    "standings": True,
                    "players": True,
                    "top_scorers": True,
                    "top_assists": True,
                    "top_cards": False,
                    "injuries": True,
                    "predictions": True,
                    "odds": True,
                },
            }
        ],
    }


def _router(paths: list[str]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        page = request.url.params.get("page", "1")
        path = request.url.path
        if path == "/timezone":
            return httpx.Response(200, json=_envelope(["UTC", "Europe/London"]))
        if path == "/countries":
            if page == "1":
                return httpx.Response(
                    200,
                    json=_envelope(
                        [{"name": "Poland", "code": "PL", "flag": None}],
                        current=1,
                        total=2,
                    ),
                )
            return httpx.Response(
                200,
                json=_envelope(
                    [{"name": "England", "code": "GB", "flag": None}],
                    current=2,
                    total=2,
                ),
            )
        if path == "/teams/countries":
            return httpx.Response(
                200,
                json=_envelope([{"name": "World", "code": None, "flag": None}]),
            )
        if path == "/leagues/seasons":
            return httpx.Response(200, json=_envelope([2023, 2024]))
        if path == "/leagues":
            return httpx.Response(200, json=_envelope([_league_payload()]))
        if path == "/odds/bookmakers":
            return httpx.Response(200, json=_envelope([{"id": 8, "name": "Bet365"}]))
        if path == "/odds/bets":
            return httpx.Response(
                200,
                json=_envelope(
                    [{"id": 311, "name": "Which team will score the 1st goal?"}]
                ),
            )
        if path == "/odds/live/bets":
            return httpx.Response(
                200,
                json=_envelope(
                    [
                        {"id": 1, "name": "Match Winner"},
                        {
                            "id": 73,
                            "name": "Which team will score the 1st goal?",
                        },
                        {
                            "id": 85,
                            "name": "Which team will score the 2nd goal?",
                        },
                    ]
                ),
            )
        raise AssertionError(f"unexpected path {path}")

    return handler


async def _reset_dictionary_tasks(
    factory: async_sessionmaker[AsyncSession],
    endpoints: Sequence[str] | None = None,
) -> None:
    chosen = endpoints or DICTIONARY_ENDPOINTS
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(EtlTask)
                .where(EtlTask.endpoint.in_(chosen))
                .values(status="pending", completed_at=None, params={})
            )


def _client(settings: Settings, transport: httpx.BaseTransport) -> FootballClient:
    return FootballClient(
        settings,
        locked=True,
        transport=transport,
        retry_wait=WaitZero(),
    )


@pytest.mark.asyncio
async def test_w3_dictionaries_are_upserted_and_paged() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = _client(settings, httpx.MockTransport(_router(paths)))
    try:
        await _reset_dictionary_tasks(factory)
        ingest = CatalogIngest(client, factory)
        await ingest.refresh_priority_one()
        after_first = list(paths)
        count_after_first: int | None
        async with factory() as session:
            count_after_first = await session.scalar(
                select(func.count()).select_from(Country)
            )
        await ingest.refresh_priority_one()
        assert paths == after_first

        await _reset_dictionary_tasks(factory)
        await ingest.refresh_priority_one()

        async with factory() as session:
            country_names = set(await session.scalars(select(Country.name)))
            years = set(await session.scalars(select(Season.year)))
            league = await session.get(League, 39)
            coverage = await session.get(LeagueSeason, (39, 2024))
            book = await session.get(Bookmaker, 8)
            pre = await session.get(OddsBet, 311)
            live = await session.get(OddsLiveBet, 85)
            live_first = await session.get(OddsLiveBet, 73)
            live_same_id = await session.get(OddsLiveBet, 1)
            task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/odds/live/bets")
            )
            country_count = await session.scalar(
                select(func.count()).select_from(Country)
            )

        assert ingest.timezones == ["UTC", "Europe/London"]
        assert "Poland" in country_names
        assert "England" in country_names
        assert "World" in country_names
        assert {2023, 2024}.issubset(years)
        assert league is not None
        assert league.country_name == "England"
        assert coverage is not None
        assert coverage.cov_events is True
        assert coverage.cov_statistics_fixtures is False
        assert coverage.cov_odds is True
        assert book is not None
        assert pre is not None
        assert pre.name == "Which team will score the 1st goal?"
        assert live is not None
        assert live_first is not None
        assert live_first.name == "Which team will score the 1st goal?"
        assert live_same_id is not None
        assert live_same_id.name == "Match Winner"
        assert task is not None
        assert task.status == "complete"
        assert task.paging_current == 1
        assert task.paging_total == 1
        assert task.params["next_goal_bet_ids"] == [73, 85]
        assert 311 not in task.params["next_goal_bet_ids"]
        assert "/fixtures" not in paths
        assert after_first.count("/countries") == 2
        assert paths.count("/countries") == 4
        assert country_count == count_after_first
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_valid_response_is_complete() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/timezone"
        return httpx.Response(200, json=_envelope([]))

    client = _client(settings, httpx.MockTransport(handler))
    try:
        await _reset_dictionary_tasks(factory, ["/timezone"])
        ingest = CatalogIngest(client, factory)
        await ingest._ingest_endpoint("/timezone", ingest._persist_timezone)
        async with factory() as session:
            task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/timezone")
            )
        assert ingest.timezones == []
        assert task is not None
        assert task.status == "complete"
        assert needs_refresh(task, ingest._now()) is False
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_persist_failure_rolls_back_and_is_retryable() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_envelope([{"name": "RollbackLand", "code": "RL", "flag": None}]),
        )

    async def boom(session: AsyncSession, items: list[Any]) -> dict[str, Any]:
        await upsert_countries(
            session, [CountryItem(name="RollbackLand", code="RL", flag=None)]
        )
        raise RuntimeError("boom")

    client = _client(settings, httpx.MockTransport(handler))
    try:
        await _reset_dictionary_tasks(factory, ["/countries"])
        ingest = CatalogIngest(client, factory)
        await ingest._ingest_endpoint("/countries", boom)
        async with factory() as session:
            leftover = await session.scalar(
                select(Country).where(Country.name == "RollbackLand")
            )
            task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/countries")
            )
        assert leftover is None
        assert task is not None
        assert task.status == "retryable_error"
        assert task.last_error == "persist_failed"
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_quota_during_paging_does_not_complete() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json=_envelope(
                [{"name": "QuotaLand", "code": "QL", "flag": None}],
                current=1,
                total=3,
            ),
            headers={
                "x-ratelimit-requests-limit": "100",
                "x-ratelimit-requests-remaining": "0",
            },
        )

    client = _client(settings, httpx.MockTransport(handler))
    try:
        await _reset_dictionary_tasks(factory, ["/countries"])
        ingest = CatalogIngest(client, factory)
        await ingest._ingest_endpoint("/countries", ingest._persist_countries)
        async with factory() as session:
            leftover = await session.scalar(
                select(Country).where(Country.name == "QuotaLand")
            )
            task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/countries")
            )
        assert calls["n"] == 1
        assert leftover is None
        assert task is not None
        assert task.status == "retryable_error"
        assert task.last_error == "quota_exhausted"
        assert task.completed_at is None
    finally:
        await client.aclose()
        await engine.dispose()
