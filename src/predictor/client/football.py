"""httpx client for API-Football. Domain GETs require the writer lock."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
)
from tenacity.wait import wait_base

from predictor.client.errors import (
    AuthBlockedError,
    FootballHttpError,
    RetryableHttpError,
    WriterLockRequiredError,
)
from predictor.client.quota import QuotaSnapshot, quota_from_http
from predictor.client.retry import WaitRetryAfterOrJitter, parse_retry_after
from predictor.constants import (
    API_SPORTS_KEY_HEADER,
    HTTP_CONNECT_TIMEOUT_SECONDS,
    HTTP_MAX_CONNECTIONS,
    HTTP_MAX_KEEPALIVE_CONNECTIONS,
    HTTP_POOL_TIMEOUT_SECONDS,
    HTTP_READ_TIMEOUT_SECONDS,
    HTTP_RETRY_ATTEMPTS,
    HTTP_WRITE_TIMEOUT_SECONDS,
)
from predictor.schemas.api_football import ApiEnvelope
from predictor.schemas.settings import Settings


class FootballClient:
    def __init__(
        self,
        settings: Settings,
        *,
        locked: bool,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
        retry_wait: wait_base | None = None,
    ) -> None:
        self._key = settings.api_sports_key.get_secret_value()
        self._locked = locked
        self._auth_blocked = False
        self._quota: QuotaSnapshot | None = None
        self._wait = retry_wait or WaitRetryAfterOrJitter()
        timeout = httpx.Timeout(
            connect=HTTP_CONNECT_TIMEOUT_SECONDS,
            read=HTTP_READ_TIMEOUT_SECONDS,
            write=HTTP_WRITE_TIMEOUT_SECONDS,
            pool=HTTP_POOL_TIMEOUT_SECONDS,
        )
        limits = httpx.Limits(
            max_connections=HTTP_MAX_CONNECTIONS,
            max_keepalive_connections=HTTP_MAX_KEEPALIVE_CONNECTIONS,
        )
        headers = {
            API_SPORTS_KEY_HEADER: self._key,
            "Accept": "application/json",
        }
        client_kwargs: dict[str, Any] = {
            "base_url": settings.api_sports_base_url.rstrip("/"),
            "headers": headers,
            "timeout": timeout,
            "limits": limits,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._http = httpx.AsyncClient(**client_kwargs)

    def __repr__(self) -> str:
        return f"FootballClient(locked={self._locked})"

    @property
    def auth_blocked(self) -> bool:
        return self._auth_blocked

    @property
    def quota(self) -> QuotaSnapshot | None:
        return self._quota

    def _redact(self, text: str) -> str:
        if self._key and self._key in text:
            return text.replace(self._key, "***")
        return text

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(
        self,
        path: str,
        *,
        domain: bool = True,
        params: Mapping[str, str | int] | None = None,
    ) -> httpx.Response:
        return await self.request("GET", path, domain=domain, params=params)

    async def request(
        self,
        method: str,
        path: str,
        *,
        domain: bool = True,
        params: Mapping[str, str | int] | None = None,
    ) -> httpx.Response:
        del domain  # lock gates every call, including /status
        if not self._locked:
            raise WriterLockRequiredError(path)
        if self._auth_blocked:
            raise AuthBlockedError(
                "API authorization previously failed; new requests are stopped"
            )

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(HTTP_RETRY_ATTEMPTS),
            retry=retry_if_exception_type((RetryableHttpError, httpx.TransportError)),
            wait=self._wait,
            reraise=True,
        ):
            with attempt:
                return await self._send_once(method, path, params)
        raise RuntimeError("unreachable retry loop")

    async def _send_once(
        self,
        method: str,
        path: str,
        params: Mapping[str, str | int] | None,
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, path, params=params)
        except httpx.TransportError:
            raise RetryableHttpError(None) from None

        self._note_quota_headers(response)
        if response.status_code in {401, 403}:
            self._auth_blocked = True
            raise AuthBlockedError(
                f"API authorization failed: HTTP {response.status_code} for {path}"
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableHttpError(
                response.status_code,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if response.status_code >= 400:
            raise FootballHttpError(response.status_code, path)
        return response

    def _note_quota_headers(self, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-requests-remaining")
        limit = response.headers.get("x-ratelimit-requests-limit")
        if remaining is None or limit is None:
            return
        try:
            remaining_i = int(remaining)
            limit_i = int(limit)
        except ValueError:
            return
        current_raw = response.headers.get("x-ratelimit-requests")
        current_i = (
            int(current_raw)
            if current_raw and current_raw.isdigit()
            else max(0, limit_i - remaining_i)
        )

        self._quota = QuotaSnapshot(
            current=current_i,
            limit_day=limit_i,
            remaining=max(0, remaining_i),
            fetched_at=datetime.now(UTC),
            source="api",
        )

    async def get_status(self) -> QuotaSnapshot:
        response = await self.get("/status", domain=False)
        try:
            payload: Any = response.json()
            envelope = ApiEnvelope.model_validate(payload)
            snapshot = quota_from_http(response, envelope)
        except (ValueError, ValidationError) as exc:
            raise FootballHttpError(response.status_code, "/status") from exc
        self._quota = snapshot
        return snapshot
