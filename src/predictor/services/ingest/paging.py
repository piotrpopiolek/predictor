"""Fetch every page of a paginated API-Football response (FR-008)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from predictor.client.errors import FootballHttpError, QuotaExhaustedError
from predictor.client.football import FootballClient
from predictor.schemas.api_football import ApiEnvelope, envelope_has_errors


def parse_http_envelope(payload: Any, *, path: str, status_code: int) -> ApiEnvelope:
    try:
        return ApiEnvelope.model_validate(payload)
    except (ValueError, ValidationError) as exc:
        raise FootballHttpError(status_code, path) from exc


async def fetch_all_pages(
    client: FootballClient,
    path: str,
    *,
    params: Mapping[str, str | int] | None = None,
) -> tuple[list[Any], int, int]:
    collected: list[Any] = []
    page = 1
    last_current = 0
    total = 1
    current = 1
    while True:
        quota = client.quota
        if quota is not None and quota.remaining <= 0 and page > 1:
            raise QuotaExhaustedError(path)
        query: dict[str, str | int] = dict(params or {})
        query["page"] = page
        response = await client.get(path, domain=True, params=query)
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise FootballHttpError(response.status_code, path) from exc
        envelope = parse_http_envelope(
            payload, path=path, status_code=response.status_code
        )
        if envelope_has_errors(envelope.errors):
            raise FootballHttpError(response.status_code, path)
        chunk = envelope.response
        if isinstance(chunk, list):
            collected.extend(chunk)
        elif chunk is not None:
            collected.append(chunk)
        paging = envelope.paging
        current = paging.current if paging and paging.current is not None else 1
        total = paging.total if paging and paging.total is not None else 1
        if current >= total or current == last_current:
            break
        last_current = current
        page = current + 1
        if page > total:
            break
    return collected, current, total
