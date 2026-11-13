"""Pure cases for this layer. They do not open Postgres."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.football import FootballClient
from predictor.schemas.settings import Settings, load_settings
from predictor.services.ingest.candidate_refresh import persist_targeted_live

_LEAGUE = 991_401

_HOME = 991_402

_AWAY = 991_403

_FIXTURE = 991_404

_BET = 991_405

_NOW = datetime(2026, 11, 13, 16, 0, tzinfo=UTC)

_QUOTA = "You have reached the request limit for the day"


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


@pytest.mark.asyncio
async def test_empty_id_list_does_not_call_the_api() -> None:
    paths: list[str] = []
    settings = load_settings()
    client = _client(settings, _ok(paths))

    def factory() -> None:
        raise AssertionError("empty id list opened a session")

    try:
        assert await persist_targeted_live(client, cast(Any, factory), []) is True
        assert paths == []
        assert client.calls_made == 0
    finally:
        await client.aclose()
