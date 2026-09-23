from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.errors import (
    AuthBlockedError,
    FootballHttpError,
    QuotaExhaustedError,
    RetryableHttpError,
    WriterLockRequiredError,
)
from predictor.client.football import FootballClient
from predictor.client.quota import quota_from_http, requests_limit_reached
from predictor.client.retry import WaitRetryAfterOrJitter, parse_retry_after
from predictor.schemas.api_football import ApiEnvelope
from predictor.schemas.settings import load_settings

STATUS_BODY = {
    "get": "status",
    "errors": [],
    "results": 1,
    "paging": {"current": 1, "total": 1},
    "response": {"requests": {"current": 12, "limit_day": 100}},
}


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


def _client(locked: bool, transport: httpx.BaseTransport) -> FootballClient:
    return FootballClient(
        load_settings(),
        locked=locked,
        transport=transport,
        retry_wait=WaitZero(),
    )


@pytest.mark.asyncio
async def test_domain_get_without_lock_does_not_http(valid_env: None) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=STATUS_BODY)

    client = _client(False, httpx.MockTransport(handler))
    try:
        with pytest.raises(WriterLockRequiredError):
            await client.get("/fixtures", domain=True)
        with pytest.raises(WriterLockRequiredError):
            await client.get("/status", domain=False)
    finally:
        await client.aclose()
    assert calls == []


@pytest.mark.asyncio
async def test_status_quota_from_body(valid_env: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/status"
        assert request.headers["x-apisports-key"] == "test-api-key-not-real"
        return httpx.Response(200, json=STATUS_BODY)

    client = _client(True, httpx.MockTransport(handler))
    try:
        snapshot = await client.get_status()
    finally:
        await client.aclose()
    assert snapshot.current == 12
    assert snapshot.limit_day == 100
    assert snapshot.remaining == 88
    assert "test-api-key-not-real" not in repr(client)


@pytest.mark.asyncio
async def test_quota_headers_override_body(valid_env: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=STATUS_BODY,
            headers={
                "x-ratelimit-requests-limit": "7500",
                "x-ratelimit-requests-remaining": "10",
            },
        )

    client = _client(True, httpx.MockTransport(handler))
    try:
        snapshot = await client.get_status()
    finally:
        await client.aclose()
    assert snapshot.limit_day == 7500
    assert snapshot.remaining == 10


LIMIT_BODY = {
    "get": "status",
    "errors": {
        "requests": (
            "You have reached the request limit for the day, "
            "Go to https://dashboard.api-football.com to upgrade your plan."
        )
    },
    "results": 0,
    "response": [],
}


def test_requests_limit_reached_ignores_other_errors() -> None:
    assert requests_limit_reached(LIMIT_BODY["errors"]) is True
    assert requests_limit_reached([]) is False
    assert requests_limit_reached({"token": "missing"}) is False
    assert requests_limit_reached({"requests": "plan does not include this"}) is False


def test_quota_from_http_trusts_daily_limit_body_over_header() -> None:
    response = httpx.Response(
        200,
        json=LIMIT_BODY,
        headers={
            "x-ratelimit-requests-limit": "7500",
            "x-ratelimit-requests-remaining": "7499",
        },
    )
    snapshot = quota_from_http(response, ApiEnvelope.model_validate(LIMIT_BODY))
    assert snapshot.remaining == 0
    assert snapshot.limit_day == 7500
    assert snapshot.current == 7500


@pytest.mark.asyncio
async def test_daily_limit_body_stops_further_http(valid_env: None) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json=LIMIT_BODY,
            headers={
                "x-ratelimit-requests-limit": "7500",
                "x-ratelimit-requests-remaining": "7499",
            },
        )

    client = _client(True, httpx.MockTransport(handler))
    try:
        with pytest.raises(QuotaExhaustedError):
            await client.get("/fixtures", domain=True)
        with pytest.raises(QuotaExhaustedError):
            await client.get("/fixtures", domain=True)
    finally:
        await client.aclose()
    assert calls["n"] == 1
    assert client.quota is not None
    assert client.quota.remaining == 0
    assert client.quota.current == 7500


@pytest.mark.asyncio
async def test_401_stops_new_requests(valid_env: None) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"message": "Invalid API Key"})

    client = _client(True, httpx.MockTransport(handler))
    try:
        with pytest.raises(AuthBlockedError, match="401"):
            await client.get("/status", domain=False)
        with pytest.raises(AuthBlockedError, match="previously failed"):
            await client.get("/status", domain=False)
    finally:
        await client.aclose()
    assert calls["n"] == 1
    assert client.auth_blocked is True


@pytest.mark.asyncio
async def test_retry_after_zero_then_success(valid_env: None) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=STATUS_BODY)

    client = _client(True, httpx.MockTransport(handler))
    try:
        response = await client.get("/status", domain=False)
    finally:
        await client.aclose()
    assert response.status_code == 200
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds(valid_env: None) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=STATUS_BODY)

    client = _client(True, httpx.MockTransport(handler))
    try:
        response = await client.get("/status", domain=False)
    finally:
        await client.aclose()
    assert response.status_code == 200
    assert calls["n"] == 3


def test_client_redacts_key(valid_env: None) -> None:
    client = FootballClient(load_settings(), locked=True)
    redacted = client._redact("header x-apisports-key=test-api-key-not-real trailing")
    assert "test-api-key-not-real" not in redacted
    assert "***" in redacted
    assert client._redact("plain") == "plain"
    assert "test-api-key-not-real" not in repr(client)


@pytest.mark.asyncio
async def test_400_is_not_retried(valid_env: None) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400)

    client = _client(True, httpx.MockTransport(handler))
    try:
        with pytest.raises(FootballHttpError) as exc_info:
            await client.get("/status", domain=False)
    finally:
        await client.aclose()
    assert exc_info.value.status_code == 400
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_invalid_status_json(valid_env: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    client = _client(True, httpx.MockTransport(handler))
    try:
        with pytest.raises(FootballHttpError):
            await client.get_status()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_transport_error_is_retryable(valid_env: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client(True, httpx.MockTransport(handler))
    try:
        with pytest.raises(RetryableHttpError):
            await client.get("/status", domain=False)
    finally:
        await client.aclose()


def test_parse_retry_after_seconds() -> None:
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("  ") is None


def test_parse_retry_after_http_date() -> None:
    future = datetime.now(UTC) + timedelta(seconds=30)
    value = format_datetime(future)
    parsed = parse_retry_after(value)
    assert parsed is not None
    assert 0 <= parsed <= 40


def test_wait_prefers_retry_after() -> None:
    wait = WaitRetryAfterOrJitter()

    class _Outcome:
        failed = True

        def exception(self) -> RetryableHttpError:
            return RetryableHttpError(429, retry_after=7.0)

    class _State:
        outcome = _Outcome()

    assert wait(_State()) == 7.0  # type: ignore[arg-type]
