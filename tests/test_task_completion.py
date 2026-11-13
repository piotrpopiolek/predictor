"""Task completion: which failure waits, which stops, and which is left alone."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from predictor.constants import (
    ERROR_RETRY_BASE_BACKOFF_SECONDS,
    ERROR_RETRY_LIMIT,
    LIVE_CONTEXT_EMPTY_BACKOFF_SECONDS,
    LIVE_CONTEXT_MAX_EMPTY_ATTEMPTS,
    MINUTE_RATE_LIMIT_RETRY_SECONDS,
)
from predictor.models.etl import EtlTask
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import load_settings
from predictor.services.ingest.task_completion import (
    fail_task,
    finish_task,
    release_in_progress_task,
    retry_or_empty_task,
)
from predictor.services.queue import complete_task

_ENDPOINT = "/minute-rate-test"
_LEAGUE = {"league": 9889013, "season": 2099}

SessionFactory = async_sessionmaker[AsyncSession]


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


def _open() -> tuple[AsyncEngine, SessionFactory]:
    engine = make_async_engine(load_settings())
    return engine, make_session_factory(engine)


async def _insert(
    factory: SessionFactory,
    endpoint: str,
    *,
    status: str = "in_progress",
    params: dict[str, object] | None = None,
    last_error: str | None = None,
) -> int:
    async with factory() as session, session.begin():
        await session.execute(delete(EtlTask).where(EtlTask.endpoint == endpoint))
        task = EtlTask(
            endpoint=endpoint,
            params=dict(params or _LEAGUE),
            status=status,
            last_error=last_error,
        )
        session.add(task)
        await session.flush()
        return int(task.id)


async def _load(factory: SessionFactory, task_id: int) -> EtlTask:
    async with factory() as session:
        stored = await session.get(EtlTask, task_id)
    assert stored is not None
    return stored


async def _drop(factory: SessionFactory, endpoint: str) -> None:
    async with factory() as session, session.begin():
        await session.execute(delete(EtlTask).where(EtlTask.endpoint == endpoint))


def _due(params: dict[str, object]) -> datetime:
    due = datetime.fromisoformat(str(params["next_attempt_at"]))
    if due.tzinfo is None:
        return due.replace(tzinfo=UTC)
    return due.astimezone(UTC)


@pytest.mark.asyncio
async def test_retryable_error_counts_then_becomes_permanent() -> None:
    endpoint = "/fail-task-retry"
    engine, factory = _open()
    try:
        task_id = await _insert(factory, endpoint)
        started = datetime.now(UTC)
        await fail_task(factory, task_id, "retryable_error", "FootballHttpError: 400")
        finished = datetime.now(UTC)
        stored = await _load(factory, task_id)
        wait = timedelta(seconds=ERROR_RETRY_BASE_BACKOFF_SECONDS)
        assert stored.status == "retryable_error"
        assert stored.last_error == "FootballHttpError: 400"
        assert stored.params["error_retries"] == 1
        assert stored.params["league"] == 9889013
        assert started + wait <= _due(stored.params) <= finished + wait
        for _ in range(ERROR_RETRY_LIMIT):
            await fail_task(
                factory, task_id, "retryable_error", "FootballHttpError: 400"
            )
        stored = await _load(factory, task_id)
        assert stored.status == "permanent_error"
        assert stored.last_error == "FootballHttpError: 400"
        assert stored.params["error_retries"] == ERROR_RETRY_LIMIT + 1
        assert "next_attempt_at" not in stored.params
        assert stored.params["league"] == 9889013
        assert stored.completed_at is not None
    finally:
        await _drop(factory, endpoint)
        await engine.dispose()


@pytest.mark.asyncio
async def test_quota_exhausted_does_not_count_as_a_retry() -> None:
    endpoint = "/fail-task-quota"
    parked = "2026-11-13T16:00:00+00:00"
    engine, factory = _open()
    try:
        task_id = await _insert(
            factory,
            endpoint,
            params={**_LEAGUE, "error_retries": 2, "next_attempt_at": parked},
        )
        for _ in range(ERROR_RETRY_LIMIT + 1):
            await fail_task(factory, task_id, "retryable_error", "quota_exhausted")
        stored = await _load(factory, task_id)
        assert stored.status == "retryable_error"
        assert stored.last_error == "quota_exhausted"
        assert stored.params["error_retries"] == 2
        assert stored.params["next_attempt_at"] == parked
        assert stored.params["league"] == 9889013
        assert stored.completed_at is None
    finally:
        await _drop(factory, endpoint)
        await engine.dispose()


@pytest.mark.asyncio
async def test_missing_task_is_left_alone() -> None:
    endpoint = "/fail-task-missing"
    engine, factory = _open()
    try:
        task_id = await _insert(factory, endpoint)
        await _drop(factory, endpoint)
        await fail_task(factory, task_id, "retryable_error", "FootballHttpError: 400")
        await release_in_progress_task(factory, task_id)
        await finish_task(factory, task_id, "complete", params={"page": 2})
        await retry_or_empty_task(
            factory, task_id, {"page": 2}, now=datetime(2026, 11, 13, tzinfo=UTC)
        )
        async with factory() as session:
            assert await session.get(EtlTask, task_id) is None
    finally:
        await _drop(factory, endpoint)
        await engine.dispose()


@pytest.mark.asyncio
async def test_release_returns_only_an_in_progress_task() -> None:
    endpoint = "/fail-task-release"
    engine, factory = _open()
    try:
        claimed = await _insert(factory, endpoint, last_error="FootballHttpError: 500")
        await release_in_progress_task(factory, claimed)
        stored = await _load(factory, claimed)
        assert stored.status == "pending"
        assert stored.last_error == "FootballHttpError: 500"
        assert stored.params["league"] == 9889013

        done = await _insert(factory, endpoint, status="complete", last_error="keep")
        await release_in_progress_task(factory, done)
        stored = await _load(factory, done)
        assert stored.status == "complete"
        assert stored.last_error == "keep"

        waiting = await _insert(factory, endpoint, status="pending")
        await release_in_progress_task(factory, waiting)
        stored = await _load(factory, waiting)
        assert stored.status == "pending"
    finally:
        await _drop(factory, endpoint)
        await engine.dispose()


@pytest.mark.asyncio
async def test_finish_task_merges_params_and_clears_the_error() -> None:
    endpoint = "/fail-task-finish"
    engine, factory = _open()
    try:
        task_id = await _insert(factory, endpoint, last_error="FootballHttpError: 400")
        await finish_task(
            factory,
            task_id,
            "pending",
            params={"page": 2},
            paging_current=1,
            paging_total=4,
        )
        stored = await _load(factory, task_id)
        assert stored.status == "pending"
        assert stored.last_error is None
        assert stored.params["league"] == 9889013
        assert stored.params["season"] == 2099
        assert stored.params["page"] == 2
        assert stored.paging_current == 1
        assert stored.paging_total == 4
        assert stored.completed_at is None
    finally:
        await _drop(factory, endpoint)
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_retry_backs_off_then_stops() -> None:
    endpoint = "/fail-task-empty"
    now = datetime(2026, 11, 13, 16, 0, tzinfo=UTC)
    engine, factory = _open()
    try:
        task_id = await _insert(factory, endpoint, last_error="old")
        await retry_or_empty_task(factory, task_id, {"team": 7}, now=now)
        stored = await _load(factory, task_id)
        assert stored.status == "retryable_error"
        assert stored.last_error == "empty_retry"
        assert stored.params["empty_attempts"] == 1
        assert stored.params["league"] == 9889013
        assert stored.params["team"] == 7
        assert (
            stored.params["next_attempt_at"]
            == (now + timedelta(seconds=LIVE_CONTEXT_EMPTY_BACKOFF_SECONDS)).isoformat()
        )
        for _ in range(LIVE_CONTEXT_MAX_EMPTY_ATTEMPTS - 1):
            await retry_or_empty_task(factory, task_id, {}, now=now)
        stored = await _load(factory, task_id)
        assert stored.status == "coverage_empty"
        assert stored.params["empty_attempts"] == LIVE_CONTEXT_MAX_EMPTY_ATTEMPTS
        assert "next_attempt_at" not in stored.params
        assert stored.params["team"] == 7
        assert stored.last_error == "empty_retry"
        assert stored.completed_at is not None
    finally:
        await _drop(factory, endpoint)
        await engine.dispose()
