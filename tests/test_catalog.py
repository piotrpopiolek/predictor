from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.errors import FootballHttpError, QuotaExhaustedError
from predictor.client.football import FootballClient
from predictor.constants import ODDS_MAPPING_RETRY_BACKOFF_SECONDS
from predictor.logutil import configure_logging
from predictor.models.etl import EtlTask
from predictor.schemas.catalog import CountryItem
from predictor.schemas.enrichment import FixtureDetail
from predictor.schemas.fixtures import FixtureItem
from predictor.schemas.odds import OddsMappingItem
from predictor.schemas.settings import load_settings
from predictor.services.ingest.drift import warn_model_extra
from predictor.services.ingest.global_entities import _parse_many, _parse_player_items
from predictor.services.ingest.next_goal import is_next_goal_market, next_goal_matches
from predictor.services.ingest.paging import fetch_all_pages
from predictor.services.queue import mapping_needs_refresh, needs_refresh


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
async def test_fetch_all_pages_stops_after_max_pages(valid_env: None) -> None:
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        calls.append(page)
        current = int(page or "1")
        return httpx.Response(
            200,
            json=_envelope([{"id": current}], current=current, total=5),
        )

    client = _client(httpx.MockTransport(handler))
    try:
        items, current, total = await fetch_all_pages(
            client, "/players", start_page=3, max_pages=1
        )
    finally:
        await client.aclose()
    assert calls == ["3"]
    assert items == [{"id": 3}]
    assert current == 3
    assert total == 5


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


@pytest.mark.asyncio
async def test_unsupported_page_keeps_the_rows_already_fetched(
    valid_env: None,
) -> None:
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        calls.append(page)
        if page is None:
            return httpx.Response(
                200,
                json=_envelope([{"name": "Poland"}], current=1, total=3),
            )
        return httpx.Response(
            200,
            json=_envelope(
                [],
                errors={"page": "The Page field do not exist."},
            ),
        )

    client = _client(httpx.MockTransport(handler))
    try:
        items, current, total = await fetch_all_pages(client, "/countries")
    finally:
        await client.aclose()
    assert calls == [None, "2"]
    assert items == [{"name": "Poland"}]
    assert current == 1
    assert total == 3


@pytest.mark.asyncio
async def test_resume_stops_when_the_vendor_rejects_the_page_field(
    valid_env: None,
) -> None:
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("page"))
        assert request.url.params.get("league") == "39"
        return httpx.Response(
            200,
            json=_envelope([], errors={"page": "The Page field DO NOT EXIST."}),
        )

    client = _client(httpx.MockTransport(handler))
    try:
        items, current, total = await fetch_all_pages(
            client,
            "/players",
            params={"league": 39, "season": 2026},
            start_page=4,
        )
    finally:
        await client.aclose()
    assert calls == ["4"]
    assert items == []
    assert current == 1
    assert total == 1


@pytest.mark.asyncio
async def test_page_phrase_on_the_first_request_is_still_an_error(
    valid_env: None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "page" not in request.url.params
        return httpx.Response(
            200,
            json=_envelope([], errors={"page": "The Page field do not exist."}),
        )

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(FootballHttpError) as raised:
            await fetch_all_pages(client, "/countries")
    finally:
        await client.aclose()
    assert raised.value.status_code == 200
    assert raised.value.path == "/countries"


@pytest.mark.asyncio
async def test_dictionary_error_is_not_an_empty_page(valid_env: None) -> None:
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        calls.append(page)
        if page is None:
            return httpx.Response(
                200,
                json=_envelope([{"name": "Poland"}], current=1, total=2),
            )
        return httpx.Response(
            200,
            json=_envelope([], errors={"league": "The League field is required."}),
        )

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(FootballHttpError) as raised:
            await fetch_all_pages(client, "/countries")
    finally:
        await client.aclose()
    assert calls == [None, "2"]
    assert raised.value.path == "/countries"


@pytest.mark.asyncio
async def test_other_page_message_is_an_error(valid_env: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_envelope([], errors={"page": ["do not exist"]}),
        )

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(FootballHttpError):
            await fetch_all_pages(client, "/players", start_page=2)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_invalid_json_and_envelope_are_football_errors(valid_env: None) -> None:
    def html(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"<html>no</html>")

    client = _client(httpx.MockTransport(html))
    try:
        with pytest.raises(FootballHttpError) as bad_json:
            await fetch_all_pages(client, "/countries")
    finally:
        await client.aclose()
    assert bad_json.value.status_code == 200
    assert bad_json.value.path == "/countries"

    def broken(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"paging": "no", "response": []})

    client = _client(httpx.MockTransport(broken))
    try:
        with pytest.raises(FootballHttpError) as bad_envelope:
            await fetch_all_pages(client, "/leagues")
    finally:
        await client.aclose()
    assert bad_envelope.value.path == "/leagues"


@pytest.mark.asyncio
async def test_object_response_is_one_row_and_null_is_empty(valid_env: None) -> None:
    bodies = iter(
        (
            _envelope({"id": 39, "name": "Premier League"}),
            _envelope(None),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=next(bodies))

    client = _client(httpx.MockTransport(handler))
    try:
        items, current, total = await fetch_all_pages(client, "/leagues")
        empty, empty_current, empty_total = await fetch_all_pages(client, "/venues")
    finally:
        await client.aclose()
    assert items == [{"id": 39, "name": "Premier League"}]
    assert current == 1
    assert total == 1
    assert empty == []
    assert empty_current == 1
    assert empty_total == 1


def test_needs_refresh_is_daily_for_complete_dictionaries() -> None:
    now = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    task = EtlTask(endpoint="/countries", params={}, status="complete")
    task.completed_at = now
    assert needs_refresh(task, now) is False
    next_day = datetime(2026, 9, 13, 0, 1, tzinfo=UTC)
    assert needs_refresh(task, next_day) is True
    pending = EtlTask(endpoint="/countries", params={}, status="pending")
    assert needs_refresh(pending, now) is True


def test_mapping_needs_refresh_skips_complete_until_two_hours() -> None:
    now = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)
    task = EtlTask(endpoint="/odds/mapping", params={}, status="complete")
    task.completed_at = now
    assert mapping_needs_refresh(task, now) is False
    almost = now + timedelta(hours=1, minutes=59)
    assert mapping_needs_refresh(task, almost) is False
    due = now + timedelta(hours=2)
    assert mapping_needs_refresh(task, due) is True
    pending = EtlTask(endpoint="/odds/mapping", params={}, status="pending")
    assert mapping_needs_refresh(pending, now) is True


def test_mapping_needs_refresh_backs_off_retryable_error() -> None:
    failed_at = datetime(2026, 9, 18, 10, 45, tzinfo=UTC)
    task = EtlTask(endpoint="/odds/mapping", params={}, status="retryable_error")
    task.updated_at = failed_at
    assert mapping_needs_refresh(task, failed_at) is False
    assert (
        mapping_needs_refresh(task, failed_at + timedelta(minutes=14, seconds=59))
        is False
    )
    due = failed_at + timedelta(seconds=ODDS_MAPPING_RETRY_BACKOFF_SECONDS)
    assert mapping_needs_refresh(task, due) is True


def test_unknown_country_field_logs_contract_drift(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    item = CountryItem.model_validate(
        {
            "name": "Poland",
            "code": "PL",
            "flag": None,
            "mystery": True,
            "api_key": "should-not-leak",
        }
    )
    warn_model_extra("/countries", item)
    text = capsys.readouterr().out
    assert item.model_extra == {"mystery": True, "api_key": "should-not-leak"}
    assert "contract_drift" in text
    assert "mystery" in text
    assert "api_key" in text
    assert "/countries" in text
    assert "should-not-leak" not in text
    assert "x-apisports-key" not in text


def test_fixture_vendor_events_and_standings_are_not_drift(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    item = FixtureItem.model_validate(
        {
            "fixture": {"id": 1, "status": {"short": "1H"}},
            "league": {
                "id": 39,
                "season": 2026,
                "standings": True,
                "events": True,
            },
            "teams": {"home": {"id": 33}, "away": {"id": 34}},
            "events": [{"type": "Goal"}],
        }
    )
    warn_model_extra("/fixtures", item)
    warn_model_extra("/fixtures", item.league)
    assert not item.model_extra
    assert not item.league.model_extra
    assert "contract_drift" not in capsys.readouterr().out


def test_odds_mapping_update_is_not_drift(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    item = OddsMappingItem.model_validate(
        {
            "fixture": {"id": 1},
            "update": "2026-09-13T15:00:00+00:00",
        }
    )
    warn_model_extra("/odds/mapping", item)
    assert not item.model_extra
    assert "contract_drift" not in capsys.readouterr().out


def test_fixture_detail_still_parses_event_list() -> None:
    detail = FixtureDetail.model_validate(
        {
            "fixture": {"id": 1},
            "league": {"id": 39, "season": 2026, "events": True},
            "teams": {"home": {"id": 33}, "away": {"id": 34}},
            "events": [{"type": "Goal", "time": {"elapsed": 12}}],
        }
    )
    assert len(detail.events) == 1
    assert detail.events[0].type == "Goal"
    assert detail.league.events is True


def test_invalid_catalog_row_is_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    parsed = _parse_many(
        [
            {"name": "Poland", "code": "PL"},
            {"code": "ES"},
            "not-a-row",
            {"name": "Spain", "code": "ES"},
        ],
        CountryItem,
        "/countries",
    )
    text = capsys.readouterr().out
    assert [item.name for item in parsed] == ["Poland", "Spain"]
    assert text.count("catalog_row_invalid") == 2
    assert text.count('"/countries"') == 2


def test_invalid_player_row_is_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    parsed = _parse_player_items(
        [
            {"player": {"id": 1, "name": "Bundle"}},
            {"id": 2, "name": "Bare"},
            {"player": {"name": "missing id"}},
            {"name": "also missing id"},
        ],
        "/players",
    )
    text = capsys.readouterr().out
    assert [item.player.id for item in parsed] == [1, 2]
    assert [item.player.name for item in parsed] == ["Bundle", "Bare"]
    assert text.count("catalog_row_invalid") == 2
    assert text.count('"/players"') == 2
