"""Live match details leave in fours."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from predictor.constants import LIVE_DETAIL_CONCURRENCY
from predictor.services.ingest.live_context import LiveContextIngest, _Spend

NOW = datetime(2026, 10, 24, 16, 0, tzinfo=UTC)


def _context() -> LiveContextIngest:
    return LiveContextIngest(
        object(),  # type: ignore[arg-type]
        enrichment=object(),  # type: ignore[arg-type]
        prematch=object(),  # type: ignore[arg-type]
        global_ingest=object(),  # type: ignore[arg-type]
        fixtures=object(),  # type: ignore[arg-type]
        now_fn=lambda: NOW,
    )


def _spend(max_calls: int) -> _Spend:
    return _Spend(
        started=NOW,
        max_calls=max_calls,
        target=60,
        fraction=0.85,
        now_fn=lambda: NOW,
        quota_fn=lambda: True,
    )


async def _peak(limit: int) -> int:
    context = _context()
    inflight = 0
    peak = 0
    release = asyncio.Event()
    ids = list(range(1, limit + 1))

    async def detail_state(live_ids: list[int]) -> dict:
        return {fixture_id: (None, {}, "2H", 20) for fixture_id in live_ids}

    async def refresh_detail(
        fixture_id: int,
        *,
        final: bool,
        match_status: str | None,
        refresh_seconds: int,
    ) -> bool:
        del fixture_id, final, match_status, refresh_seconds
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        if inflight == min(limit, LIVE_DETAIL_CONCURRENCY):
            release.set()
        await release.wait()
        inflight -= 1
        return True

    context._detail_state = detail_state  # type: ignore[method-assign]
    context._refresh_detail = refresh_detail  # type: ignore[method-assign]
    await context._refresh_details(ids, _spend(limit), final=False, limit=limit)
    return peak


@pytest.mark.asyncio
async def test_match_details_go_out_four_at_a_time() -> None:
    assert LIVE_DETAIL_CONCURRENCY == 4
    assert await _peak(8) == 4


@pytest.mark.asyncio
async def test_one_match_detail_stays_alone() -> None:
    assert await _peak(1) == 1
