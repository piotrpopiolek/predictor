from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from predictor.api.main import create_app
from predictor.constants import WRITER_LOCK_KEY
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import load_settings
from predictor.services.lock import WriterLock, advisory_lock_parts
from predictor.services.queue import record_run_end, record_run_start
from predictor.worker.main import configure_event_loop


@pytest.mark.asyncio
async def test_second_writer_does_not_acquire_lock() -> None:
    settings = load_settings()
    first = WriterLock(settings)
    second = WriterLock(settings)
    try:
        assert await first.try_acquire() is True
        assert await second.try_acquire() is False
        info = await second.holder_info()
        assert info is not None
        assert info.backend_pid == first.backend_pid
        assert info.application_name == "predictor-worker-lock"
        high, low = advisory_lock_parts()
        assert high == 0
        assert low == WRITER_LOCK_KEY
    finally:
        await first.release()
        await first.close()
        await second.close()


def test_ready_503_without_writer_lock() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["writer_lock"]["held"] is False


def test_ready_does_not_hold_writer_lock() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/ready").status_code == 503

    async def acquire() -> None:
        lock = WriterLock(load_settings())
        try:
            assert await lock.try_acquire() is True
        finally:
            await lock.release()
            await lock.close()

    configure_event_loop()
    asyncio.run(acquire())


def test_ready_ok_when_lock_and_open_run() -> None:
    configure_event_loop()
    settings = load_settings()

    async def prepare() -> tuple[WriterLock, int]:
        lock = WriterLock(settings)
        assert await lock.try_acquire() is True
        engine = make_async_engine(settings)
        try:
            factory = make_session_factory(engine)
            async with factory() as session:
                async with session.begin():
                    run_id = await record_run_start(session, settings, lock)
        finally:
            await engine.dispose()
        return lock, run_id

    lock, run_id = asyncio.run(prepare())
    try:
        with TestClient(create_app()) as client:
            response = client.get("/ready")
            status = client.get("/status")
            metrics = client.get(
                "/metrics",
                headers={
                    "Authorization": (
                        f"Bearer {settings.prometheus_metrics_token.get_secret_value()}"
                    )
                },
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["ready"] is True
        assert payload["writer_lock"]["held"] is True
        assert status.status_code == 200
        assert status.json()["lock"]["held"] is True
        assert metrics.status_code == 200
        assert "predictor_writer_lock" in metrics.text
        assert 'service="status"' in metrics.text
        assert "fixture_id" not in metrics.text
        assert "task_id" not in metrics.text
        assert "run_id" not in metrics.text
    finally:

        async def cleanup() -> None:
            engine = make_async_engine(settings)
            try:
                factory = make_session_factory(engine)
                async with factory() as session:
                    async with session.begin():
                        await record_run_end(session, run_id)
            finally:
                await engine.dispose()
            await lock.release()
            await lock.close()

        asyncio.run(cleanup())
