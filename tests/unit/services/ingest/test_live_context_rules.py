"""Pure cases for this layer. They do not open Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from predictor.services.ingest.live_context import (
    LiveContextIngest,
    context_budget_left,
    context_call_budget,
    deadline_order,
    detail_refresh_due,
    detail_service_order,
    match_refresh_seconds,
)

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=UTC)


def test_match_refresh_keeps_halftime_faster_than_the_second_half() -> None:
    assert match_refresh_seconds("HT", 45, urgent=300, relaxed=1200) == 300
    assert match_refresh_seconds("1H", 42, urgent=300, relaxed=1200) == 300
    assert match_refresh_seconds("1H", 12, urgent=300, relaxed=1200) == 1200
    assert match_refresh_seconds("2H", 70, urgent=300, relaxed=1200) == 1200


def test_detail_service_order_prefers_halftime_without_starving_stale_matches() -> None:
    now = datetime(2026, 9, 22, 18, 0, tzinfo=UTC)
    fresh = now - timedelta(seconds=30)
    stale = now - timedelta(minutes=20)
    ordered = detail_service_order(
        [
            (1, "2H", 70, fresh),
            (2, "HT", 45, fresh),
            (3, "1H", 42, fresh),
            (4, "2H", 80, stale),
        ],
        now=now,
        refresh_seconds=300,
    )
    assert ordered[:2] == [4, 2]
    again = detail_service_order(
        [
            (4, "2H", 80, now),
            (1, "2H", 70, fresh),
        ],
        now=now,
        refresh_seconds=300,
    )
    assert again == [1, 4]
    tied = [(1, "2H", 70, fresh), (2, "2H", 70, fresh)]
    assert detail_service_order(tied, now=now, refresh_seconds=300, cursor=0)[0] == 1
    assert detail_service_order(tied, now=now, refresh_seconds=300, cursor=1)[0] == 2


def test_deadline_order_puts_halftime_before_late_first_half() -> None:
    ordered = deadline_order(
        [
            (1, "1H", 12),
            (2, "HT", 45),
            (3, "1H", 40),
            (4, "2H", 70),
        ]
    )
    assert ordered == [2, 3, 1, 4]


def test_context_call_budget_caps_detail_and_oneshots() -> None:
    assert context_call_budget(detail_calls=40, oneshot_calls=12, hard_cap=80) == 52
    assert context_call_budget(detail_calls=40, oneshot_calls=12, hard_cap=30) == 30
    assert context_call_budget(detail_calls=0, oneshot_calls=0, hard_cap=10) == 0
    assert context_call_budget(detail_calls=4, oneshot_calls=2, hard_cap=0) == 0


def test_detail_refresh_waits_five_minutes_then_closes_once() -> None:
    now = datetime(2026, 9, 22, 18, 0, tzinfo=UTC)
    fresh = {"last_live_detail_at": now.isoformat()}
    assert (
        detail_refresh_due(
            task_status="complete",
            params=fresh,
            match_status="1H",
            now=now,
        )
        is False
    )
    assert (
        detail_refresh_due(
            task_status="complete",
            params=fresh,
            match_status="1H",
            now=now + timedelta(minutes=4),
        )
        is False
    )
    assert (
        detail_refresh_due(
            task_status="complete",
            params=fresh,
            match_status="1H",
            now=now + timedelta(minutes=5),
        )
        is True
    )
    assert (
        detail_refresh_due(
            task_status="complete",
            params=fresh,
            match_status="FT",
            now=now,
        )
        is True
    )
    closed = {**fresh, "final_detail_at": now.isoformat()}
    assert (
        detail_refresh_due(
            task_status="complete",
            params=closed,
            match_status="FT",
            now=now + timedelta(minutes=30),
            final=True,
        )
        is False
    )
    # Left live=all while still 2H: keep polling even if a premature stamp exists.
    assert (
        detail_refresh_due(
            task_status="complete",
            params=closed,
            match_status="2H",
            now=now,
            final=True,
        )
        is False
    )
    assert (
        detail_refresh_due(
            task_status="complete",
            params=closed,
            match_status="2H",
            now=now + timedelta(minutes=5),
            final=True,
        )
        is True
    )


def test_context_budget_stops_on_calls_time_and_quota() -> None:
    assert (
        context_budget_left(
            calls=0,
            max_calls=16,
            elapsed=10,
            target=60,
            fraction=0.85,
            quota_left=True,
        )
        is True
    )
    assert (
        context_budget_left(
            calls=16,
            max_calls=16,
            elapsed=1,
            target=60,
            fraction=0.85,
            quota_left=True,
        )
        is False
    )
    assert (
        context_budget_left(
            calls=1,
            max_calls=16,
            elapsed=51,
            target=60,
            fraction=0.85,
            quota_left=True,
        )
        is False
    )
    assert (
        context_budget_left(
            calls=0,
            max_calls=16,
            elapsed=0,
            target=60,
            fraction=0.85,
            quota_left=False,
        )
        is False
    )


@pytest.mark.asyncio
async def test_refresh_fetches_detail_before_oneshots_and_obeys_the_cap() -> None:
    from types import SimpleNamespace

    events: list[tuple[object, ...]] = []
    context = LiveContextIngest(
        cast_factory(),
        enrichment=SimpleNamespace(_client=SimpleNamespace(quota=None)),  # type: ignore[arg-type]
        prematch=SimpleNamespace(),  # type: ignore[arg-type]
        global_ingest=SimpleNamespace(),  # type: ignore[arg-type]
        fixtures=SimpleNamespace(),  # type: ignore[arg-type]
        now_fn=lambda: NOW,
        target_seconds=60,
        max_calls=30,
    )

    async def enqueue(_ids: list[int]) -> None:
        return None

    async def details(
        _ids: list[int],
        spend: object,
        *,
        final: bool,
        limit: int,
        refresh_seconds: int,
        relaxed_refresh_seconds: int | None = None,
    ) -> None:
        del refresh_seconds, relaxed_refresh_seconds
        if not spend.left() or limit <= 0:  # type: ignore[attr-defined]
            return
        events.append(("final" if final else "detail", limit))
        steps = min(limit, 1 if final else 3)
        for _ in range(steps):
            if not spend.left():  # type: ignore[attr-defined]
                break
            spend.take()  # type: ignore[attr-defined]

    async def groups(_ids: list[int]) -> tuple[list[int], list[int]]:
        return [1], [2]

    async def drain(
        _ids: list[int],
        spend: object,
        *,
        limit: int,
        stage: str,
    ) -> int:
        events.append(("oneshot", stage, limit))
        if limit > 0 and spend.left():  # type: ignore[attr-defined]
            spend.take()  # type: ignore[attr-defined]
            return limit - 1
        return limit

    async def prune(ids: list[int]) -> list[int]:
        return list(ids)

    context._enqueue = enqueue  # type: ignore[method-assign]
    context._refresh_details = details  # type: ignore[method-assign]
    context._deadline_groups = groups  # type: ignore[method-assign]
    context._drain = drain  # type: ignore[method-assign]
    context._prune_finishing = prune  # type: ignore[method-assign]

    stopped = await context.refresh(
        [1, 2],
        finishing_ids=[9],
        max_context_calls=0,
        oneshot_calls=12,
    )
    assert stopped.calls == 0
    assert events == []

    paced = await context.refresh(
        [1, 2],
        finishing_ids=[9],
        max_context_calls=5,
        oneshot_calls=12,
    )
    assert [item[0] for item in events] == ["detail", "final", "oneshot"]
    assert paced.calls == 5
    assert paced.finishing_ids == [9]


def cast_factory() -> object:
    return object()
