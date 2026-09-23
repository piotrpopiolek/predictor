"""Quota snapshot from /status JSON and/or rate-limit headers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from predictor.schemas.api_football import ApiEnvelope, StatusResponseBody


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    current: int
    limit_day: int
    remaining: int
    fetched_at: datetime | None = None
    source: str = "api"


def _header_int(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def requests_limit_reached(errors: Any) -> bool:
    """True when the body says the daily request cap is already spent.

    API-Football keeps HTTP 200 and leaves ``x-ratelimit-requests-remaining``
    at the plan size after the cap, so the header is not the budget.
    """
    if not isinstance(errors, dict):
        return False
    message = errors.get("requests")
    if not isinstance(message, str):
        return False
    folded = message.casefold()
    return "request limit" in folded and "day" in folded


def quota_from_http(response: httpx.Response, envelope: ApiEnvelope) -> QuotaSnapshot:
    remaining = _header_int(response, "x-ratelimit-requests-remaining")
    limit = _header_int(response, "x-ratelimit-requests-limit")
    current = _header_int(response, "x-ratelimit-requests")
    if requests_limit_reached(envelope.errors):
        spent = limit if limit is not None else 0
        return QuotaSnapshot(
            current=spent,
            limit_day=spent,
            remaining=0,
            fetched_at=datetime.now(UTC),
            source="api",
        )

    body = envelope.response
    if isinstance(body, dict):
        parsed = StatusResponseBody.model_validate(body)
        if limit is None:
            limit = parsed.requests.limit_day
        if current is None:
            current = parsed.requests.current

    if limit is None:
        limit = 0
    if current is None:
        current = 0
    if remaining is None:
        remaining = max(0, limit - current)

    return QuotaSnapshot(
        current=current,
        limit_day=limit,
        remaining=max(0, remaining),
        fetched_at=datetime.now(UTC),
        source="api",
    )
