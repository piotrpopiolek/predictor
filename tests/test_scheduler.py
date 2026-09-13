from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.errors import FootballHttpError
from predictor.client.football import FootballClient
from predictor.client.quota import QuotaSnapshot
from predictor.schemas.settings import load_settings
from predictor.services.scheduler import PRIORITY_ORDER, Scheduler


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


STATUS_BODY = {
    "get": "status",
    "errors": [],
    "results": 1,
    "response": {"requests": {"current": 100, "limit_day": 100}},
}


def _unused_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], object())


@pytest.mark.asyncio
async def test_scheduler_quota_exhausted_does_not_spin_http(valid_env: None) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json=STATUS_BODY,
            headers={
                "x-ratelimit-requests-limit": "100",
                "x-ratelimit-requests-remaining": "0",
            },
        )

    settings = load_settings()
    client = FootballClient(
        settings,
        locked=True,
        transport=httpx.MockTransport(handler),
        retry_wait=WaitZero(),
    )
    stop = asyncio.Event()
        frozen = datetime.now(UTC).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        scheduler = Scheduler(
            settings,
            client,
            _unused_factory(),
            idle_cap_seconds=0.05,
            status_refresh_seconds=3600,
            now_fn=lambda: frozen,
        )
    persisted = {"n": 0}

    async def fake_persist() -> None:
        persisted["n"] += 1

    scheduler._persist_cursors = fake_persist  # type: ignore[method-assign]
    try:

        async def halt() -> None:
            await asyncio.sleep(0.2)
            stop.set()

        await asyncio.gather(scheduler.run(stop), halt())
        assert calls["n"] == 1
        assert persisted["n"] >= 1
        assert scheduler.quota is not None
        assert scheduler.quota.remaining == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_priority_order_matches_fr023() -> None:
    assert [slot.priority for slot in PRIORITY_ORDER] == list(range(1, 9))
    assert PRIORITY_ORDER[0].name == "quota_and_live_dictionaries"
    assert PRIORITY_ORDER[2].name == "next_goal_snapshots"
    assert PRIORITY_ORDER[-1].name == "global_entities"


@pytest.mark.asyncio
async def test_unknown_quota_does_not_call_domain_handlers(valid_env: None) -> None:
    called = {"n": 0}

    async def domain() -> None:
        called["n"] += 1

    class _Client:
        async def get_status(self) -> QuotaSnapshot:
            raise FootballHttpError(500, "/status")

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        cast(Any, _Client()),
        _unused_factory(),
        handlers={5: domain},
        idle_cap_seconds=0.01,
    )
    scheduler._quota = QuotaSnapshot(
        current=0,
        limit_day=100,
        remaining=0,
        fetched_at=datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
        source="unknown",
    )
    await scheduler._tick(asyncio.Event())
    assert called["n"] == 0
