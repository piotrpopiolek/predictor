"""Shared ETL task completion helpers (fail / finish / empty-retry)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.models.etl import EtlTask
from predictor.services.queue import complete_task, empty_retry_status

SessionFactory = async_sessionmaker[AsyncSession]


async def fail_task(
    session_factory: SessionFactory,
    task_id: int,
    status: str,
    error: str,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            task = await session.get(EtlTask, task_id)
            if task is None:
                return
            await complete_task(session, task, status, error=error)


async def finish_task(
    session_factory: SessionFactory,
    task_id: int,
    status: str,
    *,
    params: dict[str, Any] | None = None,
    paging_current: int | None = None,
    paging_total: int | None = None,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            task = await session.get(EtlTask, task_id)
            if task is None:
                return
            merged = dict(task.params)
            if params:
                merged.update(params)
            await complete_task(
                session,
                task,
                status,
                params=merged,
                paging_current=paging_current,
                paging_total=paging_total,
            )


async def retry_or_empty_task(
    session_factory: SessionFactory,
    task_id: int,
    params: dict[str, Any],
    *,
    now: datetime,
    paging_current: int | None = None,
    paging_total: int | None = None,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            task = await session.get(EtlTask, task_id)
            if task is None:
                return
            merged = dict(task.params)
            merged.update(params)
            status, merged = empty_retry_status(merged, now=now)
            error = "empty_retry" if status == "retryable_error" else None
            await complete_task(
                session,
                task,
                status,
                error=error,
                params=merged,
                paging_current=paging_current,
                paging_total=paging_total,
            )
