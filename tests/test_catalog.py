from __future__ import annotations

from typing import Any

import httpx
import pytest
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.errors import FootballHttpError, QuotaExhaustedError
from predictor.client.football import FootballClient
from predictor.logutil import configure_logging
from predictor.schemas.catalog import CountryItem
from predictor.schemas.settings import load_settings
from predictor.services.ingest.drift import warn_model_extra
from predictor.services.ingest.next_goal import is_next_goal_market, next_goal_matches
from predictor.services.ingest.paging import fetch_all_pages


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


def _envelope(
    response: Any,
    *,
    current: int = 1,
    total: int = 1,
    errors: Any = None,
) -> dict[str, Any]:
    return {
        "get": "/countries",
        "errors": [] if errors is None else errors,
        "results": len(response) if isinstance(response, list) else 1,
        "paging": {"current": current, "total": total},
        "response": response,
    }


def _client(transport: httpx.BaseTransport) -> FootballClient:
    return FootballClient(
        load_settings(),
        locked=True,
        transport=transport,
        retry_wait=WaitZero(),
    )


def test_next_goal_matches_live_name_not_scorer() -> None:
    assert is_next_goal_market("Next Goal") is True
    assert is_next_goal_market("goal next") is True
    assert is_next_goal_market("Next Goal Scorer") is False
    assert is_next_goal_market("Match Winner") is False
    matches = next_goal_matches(
        [
            (1, "Match Winner"),
            (46, "Next Goal"),
            (1, "Next Goal"),
            (99, "Next Goal Player"),
        ]
    )
    assert [bet_id for bet_id, _name in matches] == [46, 1]


@pytest.mark.asyncio
async def test_fetch_all_pages(valid_env: None) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.params.get("page")))
        page = request.url.params.get("page", "1")
        if page == "1":
            body = _envelope(
                [{"name": "Poland", "code": "PL", "flag": None}],
                current=1,
                total=2,
            )
        else:
            body = _envelope(
                [{"name": "Spain", "code": "ES", "flag": None}],
                current=2,
                total=2,
            )
        return httpx.Response(200, json=body)

    client = _client(httpx.MockTransport(handler))
    try:
        items, current, total = await fetch_all_pages(client, "/countries")
    finally:
        await client.aclose()
    assert calls == ["1", "2"]
    assert current == 2
    assert total == 2
    assert [row["name"] for row in items] == ["Poland", "Spain"]


@pytest.mark.asyncio
async def test_paging_stops_when_quota_hits_zero(valid_env: None) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json=_envelope([{"name": "Poland"}], current=1, total=3),
            headers={
                "x-ratelimit-requests-limit": "100",
                "x-ratelimit-requests-remaining": "0",
            },
        )

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(QuotaExhaustedError):
            await fetch_all_pages(client, "/countries")
    finally:
        await client.aclose()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_envelope_errors_are_not_empty_success(valid_env: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_envelope([], errors=["rate limit reached"])
        )

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(FootballHttpError):
            await fetch_all_pages(client, "/countries")
    finally:
        await client.aclose()


def test_unknown_country_field_logs_contract_drift(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    item = CountryItem.model_validate(
        {"name": "Poland", "code": "PL", "flag": None, "mystery": True}
    )
    warn_model_extra("/countries", item)
    text = capsys.readouterr().out
    assert "contract_drift" in text
    assert "mystery" in text
    assert "/countries" in text
