from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
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
from predictor.schemas.settings import load_settings
from predictor.services.ingest import CatalogIngest


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
            return httpx.Response(
                200, json=_envelope([{"id": 8, "name": "Bet365"}])
            )
        if path == "/odds/bets":
            return httpx.Response(
                200, json=_envelope([{"id": 1, "name": "Next Goal"}])
            )
        if path == "/odds/live/bets":
            return httpx.Response(
                200,
                json=_envelope(
                    [
                        {"id": 1, "name": "Match Winner"},
                        {"id": 46, "name": "Next Goal"},
                    ]
                ),
            )
        raise AssertionError(f"unexpected path {path}")

    return handler


@pytest.mark.asyncio
async def test_w3_dictionaries_are_upserted_and_paged() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    paths: list[str] = []
    client = FootballClient(
        settings,
        locked=True,
        transport=httpx.MockTransport(_router(paths)),
        retry_wait=WaitZero(),
    )
    try:
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    update(EtlTask)
                    .where(EtlTask.endpoint.in_(DICTIONARY_ENDPOINTS))
                    .values(status="pending", completed_at=None, params={})
                )
        ingest = CatalogIngest(client, factory)
        await ingest.refresh_priority_one()
        await ingest.refresh_priority_one()

        async with factory() as session:
            country_names = set(await session.scalars(select(Country.name)))
            years = set(await session.scalars(select(Season.year)))
            league = await session.get(League, 39)
            coverage = await session.get(LeagueSeason, (39, 2024))
            book = await session.get(Bookmaker, 8)
            pre = await session.get(OddsBet, 1)
            live = await session.get(OddsLiveBet, 46)
            live_same_id = await session.get(OddsLiveBet, 1)
            task = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/odds/live/bets")
            )
            country_count = await session.scalar(select(func.count()).select_from(Country))

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
        assert pre.name == "Next Goal"
        assert live is not None
        assert live_same_id is not None
        assert live_same_id.name == "Match Winner"
        assert task is not None
        assert task.status == "complete"
        assert task.paging_current == 1
        assert task.paging_total == 1
        assert task.params["next_goal_bet_ids"] == [46]
        assert "/fixtures" not in paths
        assert paths.count("/countries") == 2
        assert country_count is not None
        second_paths = len(paths)
        assert second_paths == len(paths)
    finally:
        await client.aclose()
        await engine.dispose()
