from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.errors import FootballHttpError, QuotaExhaustedError
from predictor.client.football import FootballClient
from predictor.logutil import configure_logging
from predictor.models.etl import EtlTask
from predictor.schemas.catalog import CountryItem
from predictor.schemas.settings import load_settings
from predictor.services.ingest.drift import warn_model_extra
from predictor.services.ingest.next_goal import is_next_goal_market, next_goal_matches
from predictor.services.ingest.paging import fetch_all_pages
from predictor.services.queue import needs_refresh


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
    assert is_next_goal_market("Which team will score the 2nd goal?") is True
    assert (
        is_next_goal_market("Which team will score the 1st goal in extra time?") is True
    )
    assert is_next_goal_market("Next Goal Scorer") is False
    assert is_next_goal_market("Goal Scorer") is False
    assert is_next_goal_market("Race to the 2nd goal?") is False
    assert is_next_goal_market("Method of 2nd Goal") is False
    assert is_next_goal_market("Match Winner") is False
    matches = next_goal_matches(
        [
            (1, "Match Winner"),
            (73, "Which team will score the 1st goal?"),
            (311, "Which team will score the 1st goal?"),
            (84, "Which team will score the 2nd goal?"),
            (85, "Which team will score the 2nd goal?"),
            (99, "Next Goal Player"),
        ]
    )
    assert [bet_id for bet_id, _name in matches] == [73, 311, 84, 85]


@pytest.mark.asyncio
async def test_fetch_all_pages(valid_env: None) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        calls.append(page)
        if page is None:
            body = _envelope(
                [{"name": "Poland", "code": "PL", "flag": None}],
                current=1,
                total=2,
            )
        else:
            assert page == "2"
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
    assert calls == [None, "2"]
    assert current == 2
    assert total == 2
    assert [row["name"] for row in items] == ["Poland", "Spain"]


@pytest.mark.asyncio
async def test_fetch_all_pages_omits_page_on_single_page(valid_env: None) -> None:
    seen_page: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_page.append("page" in request.url.params)
        return httpx.Response(200, json=_envelope([{"id": 46, "name": "Next Goal"}]))

    client = _client(httpx.MockTransport(handler))
    try:
        items, current, total = await fetch_all_pages(client, "/odds/live/bets")
    finally:
        await client.aclose()
    assert seen_page == [False]
    assert current == 1
    assert total == 1
    assert items == [{"id": 46, "name": "Next Goal"}]


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
        return httpx.Response(200, json=_envelope([], errors=["rate limit reached"]))

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(FootballHttpError):
            await fetch_all_pages(client, "/countries")
    finally:
        await client.aclose()


def test_needs_refresh_is_daily_for_complete_dictionaries() -> None:
    now = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    task = EtlTask(endpoint="/countries", params={}, status="complete")
    task.completed_at = now
    assert needs_refresh(task, now) is False
    next_day = datetime(2026, 9, 13, 0, 1, tzinfo=UTC)
    assert needs_refresh(task, next_day) is True
    pending = EtlTask(endpoint="/countries", params={}, status="pending")
    assert needs_refresh(pending, now) is True


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
