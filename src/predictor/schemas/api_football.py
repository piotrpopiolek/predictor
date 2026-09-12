"""Pydantic envelope for API-Football responses. W2 parses /status only."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ApiPaging(BaseModel):
    model_config = ConfigDict(extra="ignore")

    current: int | None = None
    total: int | None = None


class ApiEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    get: str | None = None
    errors: Any = None
    results: int | None = None
    paging: ApiPaging | None = None
    response: Any = None


class StatusRequests(BaseModel):
    model_config = ConfigDict(extra="ignore")

    current: int
    limit_day: int


class StatusResponseBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    requests: StatusRequests
