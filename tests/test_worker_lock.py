from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.football import FootballClient
from predictor.models.etl import EtlTask
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import load_settings
from predictor.services.lock import WriterLock
from predictor.services.queue import ensure_cursors, get_cursor_task
from predictor.worker.main import run_worker


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


def _fail_http(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected HTTP {request.method} {request.url}")


@pytest.mark.asyncio
async def test_second_worker_exits_without_http() -> None:
    settings = load_settings()
    holder = WriterLock(settings)
    client = FootballClient(
        settings,
        locked=True,
        transport=httpx.MockTransport(_fail_http),
        retry_wait=WaitZero(),
    )
    try:
        assert await holder.try_acquire() is True
        from predictor.services.lock import LockBusyError

        with pytest.raises(LockBusyError):
            await run_worker(settings=settings, client=client)
    finally:
        await client.aclose()
        await holder.release()
        await holder.close()


@pytest.mark.asyncio
async def test_locked_worker_ensures_cursors_without_http_when_stopped() -> None:
    settings = load_settings()
    stop = asyncio.Event()
    stop.set()
    client = FootballClient(
        settings,
        locked=True,
        transport=httpx.MockTransport(_fail_http),
        retry_wait=WaitZero(),
    )
    engine = make_async_engine(settings)
    try:
        await run_worker(settings=settings, stop=stop, client=client, engine=engine)
        factory = make_session_factory(engine)
        async with factory() as session:
            kinds = set(
                await session.scalars(
                    select(EtlTask.cursor_kind).where(EtlTask.cursor_kind.is_not(None))
                )
            )
        assert kinds == {"forward", "backfill", "enrichment"}
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_restart_requeues_in_progress_without_http() -> None:
    settings = load_settings()
    stop = asyncio.Event()
    stop.set()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    endpoint = f"/w9/orphan-{uuid.uuid4().hex[:8]}"
    client = FootballClient(
        settings,
        locked=True,
        transport=httpx.MockTransport(_fail_http),
        retry_wait=WaitZero(),
    )
    try:
        async with factory() as session:
            async with session.begin():
                await ensure_cursors(session)
                session.add(EtlTask(endpoint=endpoint, params={}, status="in_progress"))
                cursor = await get_cursor_task(session, "backfill")
                cursor.params = {
                    "last_complete": "2026-09-01",
                    "next_day": "2026-08-31",
                }
        await run_worker(settings=settings, stop=stop, client=client, engine=engine)
        async with factory() as session:
            orphan = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == endpoint)
            )
            cursor = await session.scalar(
                select(EtlTask).where(EtlTask.cursor_kind == "backfill")
            )
        assert orphan is not None
        assert orphan.status == "pending"
        assert orphan.last_error == "orphaned_in_progress"
        assert cursor is not None
        assert cursor.params.get("last_complete") == "2026-09-01"
        assert cursor.params.get("next_day") == "2026-08-31"
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_lock_can_be_acquired_after_holder_disconnects() -> None:
    settings = load_settings()
    first = WriterLock(settings)
    second = WriterLock(settings)
    try:
        assert await first.try_acquire() is True
        assert await second.try_acquire() is False
        await first.close()
        third = WriterLock(settings)
        try:
            assert await third.try_acquire() is True
        finally:
            await third.release()
            await third.close()
    finally:
        await second.close()
