from __future__ import annotations

import pytest
from sqlalchemy import select

from predictor.constants import CURSOR_KINDS
from predictor.models.etl import EtlTask
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import load_settings
from predictor.services.queue import (
    claim_next,
    complete_task,
    ensure_cursors,
    requeue_orphans,
)


@pytest.mark.asyncio
async def test_ensure_cursors_are_idempotent() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                await ensure_cursors(session)
        async with factory() as session:
            async with session.begin():
                await ensure_cursors(session)
        async with factory() as session:
            kinds = set(
                await session.scalars(
                    select(EtlTask.cursor_kind).where(EtlTask.cursor_kind.is_not(None))
                )
            )
        assert kinds == set(CURSOR_KINDS)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_checkpoint_rolls_back_with_open_transaction() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            try:
                async with session.begin():
                    task = EtlTask(
                        endpoint="/w2/rollback", params={}, status="in_progress"
                    )
                    session.add(task)
                    await session.flush()
                    await complete_task(session, task, "complete")
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
        async with factory() as session:
            leftover = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/w2/rollback")
            )
        assert leftover is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_complete_task_requires_transaction() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            task = EtlTask(endpoint="/w2/no-txn", params={}, status="pending")
            with pytest.raises(RuntimeError, match="transaction"):
                await complete_task(session, task, "complete")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_requeue_orphans_and_claim_skips_cursors() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                await ensure_cursors(session)
                session.add(
                    EtlTask(endpoint="/w2/orphan", params={}, status="in_progress")
                )
                session.add(
                    EtlTask(endpoint="/w2/claim-me", params={}, status="pending")
                )
        async with factory() as session:
            async with session.begin():
                n = await requeue_orphans(session)
                assert n >= 1
                orphan = await session.scalar(
                    select(EtlTask).where(EtlTask.endpoint == "/w2/orphan")
                )
                assert orphan is not None
                assert orphan.status == "pending"
                assert orphan.last_error == "orphaned_in_progress"
                claimed_endpoints: list[str] = []
                while True:
                    claimed = await claim_next(session)
                    if claimed is None:
                        break
                    claimed_endpoints.append(claimed.endpoint)
                    assert claimed.cursor_kind is None
                    await complete_task(session, claimed, "complete")
                assert "/w2/claim-me" in claimed_endpoints
                assert "/w2/orphan" in claimed_endpoints
        async with factory() as session:
            orphan = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/w2/orphan")
            )
            done = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/w2/claim-me")
            )
        assert orphan is not None
        assert orphan.status == "complete"
        assert done is not None
        assert done.status == "complete"
        assert done.completed_at is not None
    finally:
        await engine.dispose()
