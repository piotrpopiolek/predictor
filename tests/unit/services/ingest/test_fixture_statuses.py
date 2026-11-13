"""Pure cases for this layer. They do not open Postgres."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from predictor.constants import (
    IRREGULAR_FIXTURE_STATUSES,
)
from predictor.services.ingest.fixtures import FixtureIngest


def test_irregular_statuses_match_fr014() -> None:
    assert {"PST", "CANC", "ABD", "AWD", "WO"} == IRREGULAR_FIXTURE_STATUSES


@pytest.mark.asyncio
async def test_backfill_pauses_while_pending_is_older_than_a_week(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    ingest = FixtureIngest(
        cast(Any, object()),
        cast(Any, lambda: _Session()),
        now_fn=lambda: datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
    )
    ingest._quota_left = lambda: True  # type: ignore[method-assign]

    async def today_done() -> bool:
        return True

    async def blocked(session: object, now: object = None) -> bool:
        del session, now
        return True

    called = {"day": 0}

    async def ingest_day(day: object, *, role: str) -> bool:
        del day, role
        called["day"] += 1
        return True

    async def no_stale() -> None:
        return None

    ingest._today_fixtures_complete = today_done  # type: ignore[method-assign]
    ingest._refresh_stale_fixture_days = no_stale  # type: ignore[method-assign]
    ingest._ingest_day = ingest_day  # type: ignore[method-assign]
    monkeypatch.setattr(
        "predictor.services.ingest.fixtures.historical_backlog_blocks",
        blocked,
    )
    await ingest.refresh_backfill()
    assert called["day"] == 0
