from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.errors import (
    FootballHttpError,
    QuotaExhaustedError,
    SpendWindowClosed,
)
from predictor.client.football import FootballClient
from predictor.client.quota import QuotaSnapshot
from predictor.schemas.settings import load_settings
from predictor.services.quota import (
    history_call_allowance,
    plan_live_budget,
    queue_cycle_seconds,
    seconds_until_utc_midnight,
)
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
    frozen = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
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
async def test_daily_limit_body_stops_the_scheduler(valid_env: None) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "get": "status",
                "errors": {
                    "requests": "You have reached the request limit for the day."
                },
                "results": 0,
                "response": [],
            },
            headers={
                "x-ratelimit-requests-limit": "7500",
                "x-ratelimit-requests-remaining": "7499",
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
    frozen = datetime.now(UTC).replace(hour=20, minute=0, second=0, microsecond=0)
    ran = {"n": 0}

    async def domain() -> None:
        ran["n"] += 1

    scheduler = Scheduler(
        settings,
        client,
        _unused_factory(),
        handlers={2: domain},
        idle_cap_seconds=0.05,
        status_refresh_seconds=3600,
        now_fn=lambda: frozen,
    )

    async def fake_persist() -> None:
        return None

    scheduler._persist_cursors = fake_persist  # type: ignore[method-assign]
    try:

        async def halt() -> None:
            await asyncio.sleep(0.2)
            stop.set()

        await asyncio.gather(scheduler.run(stop), halt())
    finally:
        await client.aclose()
    assert calls["n"] == 1
    assert ran["n"] == 0
    assert scheduler.quota is not None
    assert scheduler.quota.remaining == 0
    assert scheduler.quota.current == 7500


@pytest.mark.asyncio
async def test_matches_left_today_feeds_the_budget(valid_env: None) -> None:
    start = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)

    class _Client:
        async def get_status(self) -> QuotaSnapshot:
            return QuotaSnapshot(
                current=1000,
                limit_day=7500,
                remaining=2000,
                fetched_at=start,
                source="api",
            )

    async def left() -> int:
        return 800

    called = {"history": 0}

    async def history() -> None:
        called["history"] += 1

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        cast(Any, _Client()),
        _unused_factory(),
        handlers={8: history},
        now_fn=lambda: start,
        live_match_count=lambda: 0,
        matches_left_today=left,
    )
    await scheduler._tick(asyncio.Event())
    assert called["history"] == 0
    assert scheduler._gate.oneshot_calls == 0

    async def broken() -> int:
        raise RuntimeError("db")

    scheduler._matches_left_today = broken
    scheduler._quota = None
    await scheduler._tick(asyncio.Event())
    assert scheduler._left_today is None


@pytest.mark.asyncio
async def test_priority_order_matches_fr023() -> None:
    assert [slot.priority for slot in PRIORITY_ORDER] == list(range(1, 10))
    assert PRIORITY_ORDER[0].name == "quota_and_live_dictionaries"
    assert PRIORITY_ORDER[2].name == "live_context"
    assert PRIORITY_ORDER[3].name == "odds_live"
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


@pytest.mark.asyncio
async def test_scheduler_logs_when_live_interval_must_stretch(
    valid_env: None, caplog: pytest.LogCaptureFixture
) -> None:
    class _Client:
        async def get_status(self) -> QuotaSnapshot:
            return QuotaSnapshot(
                current=7496,
                limit_day=7500,
                remaining=4,
                fetched_at=datetime(2026, 9, 13, 23, 0, tzinfo=UTC),
                source="api",
            )

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        cast(Any, _Client()),
        _unused_factory(),
        idle_cap_seconds=0.01,
        now_fn=lambda: datetime(2026, 9, 13, 23, 0, tzinfo=UTC),
        live_match_count=lambda: 1,
    )
    with caplog.at_level("WARNING"):
        await scheduler._tick(asyncio.Event())
    assert "live_freshness_missed" in caplog.text
    assert scheduler.live_interval_seconds == 3600.0


@pytest.mark.asyncio
async def test_odds_live_waits_when_context_uses_the_tick(valid_env: None) -> None:
    called: list[int] = []
    start = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    late = {"on": False}

    def now() -> datetime:
        if late["on"]:
            return start + timedelta(seconds=120)
        return start

    async def context() -> None:
        called.append(3)
        late["on"] = True

    async def odds_live() -> None:
        called.append(4)

    class _Client:
        async def get_status(self) -> QuotaSnapshot:
            return QuotaSnapshot(
                current=10,
                limit_day=7500,
                remaining=7000,
                fetched_at=start,
                source="api",
            )

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        cast(Any, _Client()),
        _unused_factory(),
        handlers={3: context, 4: odds_live},
        now_fn=now,
        live_match_count=lambda: 1,
    )
    await scheduler._tick(asyncio.Event())
    assert called == [3]


@pytest.mark.asyncio
async def test_tick_stops_when_a_handler_hits_the_daily_limit(valid_env: None) -> None:
    called: list[int] = []

    async def dictionaries() -> None:
        called.append(1)
        raise QuotaExhaustedError("daily limit")

    async def scores() -> None:
        called.append(2)

    class _Client:
        quota = QuotaSnapshot(
            current=2500,
            limit_day=7500,
            remaining=5000,
            fetched_at=datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
            source="api",
        )

        async def get_status(self) -> QuotaSnapshot:
            return self.quota

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        cast(Any, _Client()),
        _unused_factory(),
        handlers={1: dictionaries, 2: scores},
        now_fn=lambda: datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
    )
    await scheduler._tick(asyncio.Event())
    assert called == [1]
    assert scheduler.quota is not None
    assert scheduler.quota.remaining == 5000


@pytest.mark.asyncio
async def test_stale_header_cannot_hide_a_fresh_status(valid_env: None) -> None:
    start = datetime(2026, 10, 8, 0, 1, tzinfo=UTC)
    called: list[int] = []

    class _Client:
        calls_made = 0
        quota: QuotaSnapshot | None = None

        async def get_status(self) -> QuotaSnapshot:
            self.calls_made += 1
            snap = QuotaSnapshot(
                current=8,
                limit_day=7500,
                remaining=7492,
                fetched_at=start,
                source="api",
            )
            self.quota = snap
            return snap

    client = _Client()

    async def dictionaries() -> None:
        called.append(1)
        client.calls_made += 8
        client.quota = QuotaSnapshot(
            current=7131,
            limit_day=7500,
            remaining=369,
            fetched_at=start + timedelta(seconds=1),
            source="api",
            reading="header",
        )

    async def history() -> None:
        called.append(8)

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        cast(Any, client),
        _unused_factory(),
        handlers={1: dictionaries, 8: history},
        now_fn=lambda: start,
        status_refresh_seconds=3600,
    )
    scheduler._quota = QuotaSnapshot(
        current=7131,
        limit_day=7500,
        remaining=369,
        fetched_at=start - timedelta(minutes=5),
        source="api",
    )
    await scheduler._tick(asyncio.Event())
    assert called == [1, 8]
    assert scheduler.quota is not None
    assert scheduler.quota.remaining == 7492


@pytest.mark.asyncio
async def test_overload_blocks_lower_priorities_until_the_slate_shrinks(
    valid_env: None,
) -> None:
    start = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    board = {"live": 400}

    class _Client:
        async def get_status(self) -> QuotaSnapshot:
            return QuotaSnapshot(
                current=0,
                limit_day=7500,
                remaining=7500,
                fetched_at=start,
                source="api",
            )

    called: list[int] = []

    async def history() -> None:
        called.append(8)

    async def odds_live() -> None:
        called.append(4)

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        cast(Any, _Client()),
        _unused_factory(),
        handlers={4: odds_live, 8: history},
        now_fn=lambda: start,
        live_match_count=lambda: board["live"],
    )
    await scheduler._tick(asyncio.Event())
    assert called == []
    assert scheduler._plan is not None
    assert scheduler._plan.overloaded is True

    board["live"] = 1
    scheduler._quota = None
    await scheduler._tick(asyncio.Event())
    assert 8 in called
    assert scheduler._plan is not None
    assert scheduler._plan.overloaded is False


def test_header_drop_is_limited_to_calls_this_tick(valid_env: None) -> None:
    start = datetime(2026, 10, 8, 0, 2, tzinfo=UTC)
    status = QuotaSnapshot(
        current=8,
        limit_day=7500,
        remaining=7492,
        fetched_at=start,
        source="api",
    )

    class _Client:
        calls_made = 8
        quota = QuotaSnapshot(
            current=16,
            limit_day=7500,
            remaining=7484,
            fetched_at=start + timedelta(seconds=1),
            source="api",
            reading="header",
        )

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        cast(Any, _Client()),
        _unused_factory(),
        now_fn=lambda: start,
    )
    scheduler._calls_baseline = 0
    assert scheduler._prefer_observed(status).remaining == 7484

    scheduler._client.quota = QuotaSnapshot(
        current=7131,
        limit_day=7500,
        remaining=369,
        fetched_at=start + timedelta(seconds=2),
        source="api",
        reading="header",
    )
    assert scheduler._prefer_observed(status).remaining == 7492


@pytest.mark.asyncio
async def test_new_utc_day_reads_status_before_handlers(valid_env: None) -> None:
    start = datetime(2026, 10, 8, 0, 0, 40, tzinfo=UTC)
    events: list[str] = []

    class _Client:
        quota: QuotaSnapshot | None = None

        async def get_status(self) -> QuotaSnapshot:
            events.append("status")
            snap = QuotaSnapshot(
                current=8,
                limit_day=7500,
                remaining=7492,
                fetched_at=start,
                source="api",
            )
            self.quota = snap
            return snap

    async def dictionaries() -> None:
        events.append("handler")

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        cast(Any, _Client()),
        _unused_factory(),
        handlers={1: dictionaries},
        now_fn=lambda: start,
        status_refresh_seconds=3600,
    )
    scheduler._quota = QuotaSnapshot(
        current=7131,
        limit_day=7500,
        remaining=376,
        fetched_at=start - timedelta(minutes=5),
        source="api",
    )
    await scheduler._tick(asyncio.Event())
    assert events == ["status", "handler"]
    assert scheduler.quota is not None
    assert scheduler.quota.remaining == 7492


@pytest.mark.asyncio
async def test_reset_lag_probes_status_for_fifteen_minutes(valid_env: None) -> None:
    moment = {"at": datetime(2026, 10, 8, 0, 5, tzinfo=UTC)}
    calls = {"status": 0}
    called: list[int] = []
    readings = [
        QuotaSnapshot(
            current=7496,
            limit_day=7500,
            remaining=4,
            fetched_at=moment["at"],
            source="api",
        ),
        QuotaSnapshot(
            current=8,
            limit_day=7500,
            remaining=7492,
            fetched_at=moment["at"] + timedelta(minutes=1),
            source="api",
        ),
    ]

    class _Client:
        quota: QuotaSnapshot | None = None

        async def get_status(self) -> QuotaSnapshot:
            calls["status"] += 1
            snap = readings[min(calls["status"] - 1, len(readings) - 1)]
            self.quota = snap
            return snap

    async def history() -> None:
        called.append(8)

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        cast(Any, _Client()),
        _unused_factory(),
        handlers={8: history},
        now_fn=lambda: moment["at"],
        idle_cap_seconds=60,
    )
    await scheduler._tick(asyncio.Event())
    assert calls["status"] == 1
    assert called == []
    assert scheduler.live_interval_seconds == 60

    moment["at"] = datetime(2026, 10, 8, 0, 6, tzinfo=UTC)
    await scheduler._tick(asyncio.Event())
    assert calls["status"] == 2
    assert called == [8]
    assert scheduler.quota is not None
    assert scheduler.quota.remaining == 7492


@pytest.mark.asyncio
async def test_spent_status_sleeps_until_midnight_after_probe(
    valid_env: None,
) -> None:
    moment = {"at": datetime(2026, 10, 8, 0, 20, tzinfo=UTC)}
    calls = {"status": 0}

    class _Client:
        quota: QuotaSnapshot | None = None

        async def get_status(self) -> QuotaSnapshot:
            calls["status"] += 1
            snap = QuotaSnapshot(
                current=7496,
                limit_day=7500,
                remaining=4,
                fetched_at=moment["at"],
                source="api",
            )
            self.quota = snap
            return snap

    async def history() -> None:
        raise AssertionError("handlers must wait for the next utc day")

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        cast(Any, _Client()),
        _unused_factory(),
        handlers={8: history},
        now_fn=lambda: moment["at"],
        idle_cap_seconds=60,
    )
    await scheduler._tick(asyncio.Event())
    assert calls["status"] == 1
    assert scheduler.live_interval_seconds == 85200

    await scheduler._tick(asyncio.Event())
    assert calls["status"] == 1


def test_history_allowance_spreads_surplus_across_cycles() -> None:
    assert queue_cycle_seconds(60, 60) == 120
    assert queue_cycle_seconds(300, 60) == 300
    assert history_call_allowance(surplus=0, seconds_left=3600, cycle_seconds=120) == 0
    assert (
        history_call_allowance(surplus=34000, seconds_left=29880, cycle_seconds=120)
        == 136
    )


def _history_client(now: datetime, *, recent: int = 0) -> Any:
    class _Client:
        quota = QuotaSnapshot(
            current=5000,
            limit_day=75000,
            remaining=70000,
            fetched_at=now,
            source="api",
            reading="status",
        )

        def __init__(self) -> None:
            self.calls = 0

        @property
        def calls_made(self) -> int:
            return self.calls

        def calls_in_recent_seconds(self, seconds: float) -> int:
            del seconds
            return recent

    return _Client()


@pytest.mark.asyncio
async def test_sleep_spends_the_history_pool(valid_env: None) -> None:
    now = datetime(2026, 10, 11, 16, 0, tzinfo=UTC)
    called: list[int] = []
    client = _history_client(now)

    async def odds_live() -> None:
        called.append(4)

    async def history() -> None:
        called.append(9)
        client.calls += 1

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        client,
        _unused_factory(),
        handlers={4: odds_live, 9: history},
        now_fn=lambda: now,
        live_match_count=lambda: 0,
    )
    scheduler._work_started = True
    scheduler._quota = client.quota
    scheduler._plan = plan_live_budget(
        live_matches=0,
        remaining=70000,
        now=now,
        limit_day=75000,
        buffer_percent=0.1,
    )
    window = 0.2
    surplus = 70000 - scheduler._plan.safety_buffer - scheduler._plan.full_reserve
    allowance = history_call_allowance(
        surplus=surplus,
        seconds_left=seconds_until_utc_midnight(now),
        cycle_seconds=queue_cycle_seconds(window, 60),
    )
    await scheduler._drain_during_sleep(asyncio.Event(), window)
    assert allowance > 10
    assert called.count(9) == allowance
    assert 4 not in called


@pytest.mark.asyncio
async def test_sleep_stops_when_the_window_ends(valid_env: None) -> None:
    now = datetime(2026, 10, 11, 23, 59, 50, tzinfo=UTC)
    client = _history_client(now)

    async def history() -> None:
        client.calls += 1
        await asyncio.sleep(0.08)

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        client,
        _unused_factory(),
        handlers={9: history},
        now_fn=lambda: now,
        live_match_count=lambda: 1,
    )
    scheduler._work_started = True
    scheduler._quota = client.quota
    scheduler._plan = plan_live_budget(
        live_matches=1,
        remaining=70000,
        now=now,
        limit_day=75000,
        buffer_percent=0.1,
    )
    started = time.monotonic()
    await scheduler._drain_during_sleep(asyncio.Event(), 0.15)
    assert 1 <= client.calls <= 4
    assert time.monotonic() - started < 0.6


@pytest.mark.asyncio
async def test_minute_cap_skips_the_history_window(valid_env: None) -> None:
    now = datetime(2026, 10, 11, 16, 0, tzinfo=UTC)
    client = _history_client(now, recent=450)

    async def history() -> None:
        client.calls += 1

    settings = load_settings()
    scheduler = Scheduler(
        settings,
        client,
        _unused_factory(),
        handlers={9: history},
        now_fn=lambda: now,
        live_match_count=lambda: 0,
    )
    scheduler._work_started = True
    scheduler._quota = client.quota
    scheduler._plan = plan_live_budget(
        live_matches=0,
        remaining=70000,
        now=now,
        limit_day=75000,
        buffer_percent=0.1,
    )
    await scheduler._drain_during_sleep(asyncio.Event(), 0.05)
    assert client.calls == 0


@pytest.mark.asyncio
async def test_closed_history_window_does_not_send(valid_env: None) -> None:
    sent = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        sent["n"] += 1
        return httpx.Response(
            200,
            json={"response": []},
            headers={
                "x-ratelimit-requests-limit": "75000",
                "x-ratelimit-requests-remaining": "70000",
            },
        )

    settings = load_settings()
    client = FootballClient(
        settings,
        locked=True,
        transport=httpx.MockTransport(handler),
        retry_wait=WaitZero(),
    )
    try:
        client.open_history_window(
            deadline=time.monotonic() + 5,
            call_ceiling=1,
            per_minute=450,
        )
        await client.get("/timezone")
        with pytest.raises(SpendWindowClosed):
            await client.get("/timezone")
        assert sent["n"] == 1
    finally:
        await client.aclose()
