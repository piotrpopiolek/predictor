"""History fetch batch: the quiet lane overlaps, one task stays alone."""

from __future__ import annotations

import asyncio

import pytest

from predictor.constants import lane_split
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
async def test_quiet_history_lane_overlaps_in_flight() -> None:
    width = lane_split(0).history_concurrency
    ingest = GlobalIngest(object(), object(), per_tick=width)  # type: ignore[arg-type]
    assert await _peak(ingest, list(range(width))) == width


@pytest.mark.asyncio
async def test_one_transfer_stays_serial() -> None:
    ingest = GlobalIngest(object(), object(), per_tick=1)  # type: ignore[arg-type]
    assert await _peak(ingest, [1]) == 1
