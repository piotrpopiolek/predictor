"""Task completion: a spent minute waits, it does not become permanent_error."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from predictor.constants import ERROR_RETRY_LIMIT, MINUTE_RATE_LIMIT_RETRY_SECONDS
from predictor.models.etl import EtlTask
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import load_settings
from predictor.services.ingest.task_completion import fail_task
from predictor.services.queue import complete_task

_ENDPOINT = "/minute-rate-test"


@pytest.mark.asyncio
async def test_minute_rate_limit_defers_without_counting_retries() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    task_id = 0
    try:
        async with factory() as session, session.begin():
            await session.execute(delete(EtlTask).where(EtlTask.endpoint == _ENDPOINT))
            task = EtlTask(
                endpoint=_ENDPOINT,
                params={"league": 9889013, "season": 2099},
                status="in_progress",
            )
            session.add(task)
            await session.flush()
            task_id = int(task.id)
        for _ in range(ERROR_RETRY_LIMIT + 1):
            await fail_task(
                factory, task_id, "retryable_error", "MinuteRateLimited: 429"
            )
        started = datetime.now(UTC)
        await fail_task(factory, task_id, "retryable_error", "MinuteRateLimited: 429")
        finished = datetime.now(UTC)
        async with factory() as session:
            stored = await session.get(EtlTask, task_id)
        assert stored is not None
        assert stored.status == "retryable_error"
        assert stored.last_error == "MinuteRateLimited: 429"
        assert "error_retries" not in stored.params
        assert stored.params["league"] == 9889013
        due = datetime.fromisoformat(str(stored.params["next_attempt_at"]))
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        wait = timedelta(seconds=MINUTE_RATE_LIMIT_RETRY_SECONDS)
        assert started + wait <= due <= finished + wait
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(EtlTask).where(EtlTask.endpoint == _ENDPOINT))
        await engine.dispose()


@pytest.mark.asyncio
async def test_successful_page_clears_last_error() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            await session.execute(delete(EtlTask).where(EtlTask.endpoint == _ENDPOINT))
            task = EtlTask(
                endpoint=_ENDPOINT,
                params={"league": 9889013, "season": 2099},
                status="in_progress",
                last_error="MinuteRateLimited: 429",
            )
            session.add(task)
            await session.flush()
            await complete_task(
                session,
                task,
                "pending",
                params={"league": 9889013, "season": 2099, "page": 2},
                paging_current=1,
                paging_total=4,
            )
            task_id = int(task.id)
        async with factory() as session:
            stored = await session.get(EtlTask, task_id)
        assert stored is not None
        assert stored.status == "pending"
        assert stored.last_error is None
        assert stored.params["page"] == 2
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(EtlTask).where(EtlTask.endpoint == _ENDPOINT))
        await engine.dispose()
