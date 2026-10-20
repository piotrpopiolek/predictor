"""History fetch batch: six independent tasks overlap, one stays alone."""

from __future__ import annotations

import asyncio

import pytest

from predictor.constants import HISTORY_FETCH_CONCURRENCY
from predictor.services.ingest.global_entities import GlobalIngest


async def _peak(ingest: GlobalIngest, task_ids: list[int]) -> int:
    inflight = 0
    peak = 0
    release = asyncio.Event()

    async def one_task(
        *, task_id: int | None = None, retry_empty: bool = False
    ) -> bool:
        del task_id, retry_empty
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        if inflight == len(task_ids):
            release.set()
        await release.wait()
        inflight -= 1
        return True

    ingest._one_task = one_task  # type: ignore[method-assign]
    await ingest._run_batch(task_ids)
    return peak


@pytest.mark.asyncio
async def test_six_transfers_overlap_in_flight() -> None:
    assert HISTORY_FETCH_CONCURRENCY == 6
    ingest = GlobalIngest(object(), object(), per_tick=6)  # type: ignore[arg-type]
    assert await _peak(ingest, [1, 2, 3, 4, 5, 6]) == 6


@pytest.mark.asyncio
async def test_one_transfer_stays_serial() -> None:
    ingest = GlobalIngest(object(), object(), per_tick=1)  # type: ignore[arg-type]
    assert await _peak(ingest, [1]) == 1
