from __future__ import annotations

import asyncio

import httpx
import pytest
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.football import FootballClient
from predictor.postgres import make_async_engine
from predictor.schemas.settings import load_settings
from predictor.services.lock import WriterLock
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
        from sqlalchemy import select

        from predictor.models.etl import EtlTask
        from predictor.postgres import make_session_factory

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
