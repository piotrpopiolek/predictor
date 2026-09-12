"""Worker process: lock first, then queue/cursors, then scheduler.

No domain ingest in W2.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncEngine

from predictor.client.errors import AuthBlockedError
from predictor.client.football import FootballClient
from predictor.logutil import configure_logging, log_json, log_startup
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings, SettingsError, load_settings
from predictor.services.lock import LockBusyError, WriterLock, lock_busy_message
from predictor.services.queue import (
    ensure_cursors,
    record_run_end,
    record_run_start,
    requeue_orphans,
)
from predictor.services.scheduler import Scheduler

LockedLoop = Callable[[Settings, WriterLock, asyncio.Event], Awaitable[None]]


def configure_event_loop() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _install_stop_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            break


async def run_locked_loop(
    settings: Settings,
    lock: WriterLock,
    stop: asyncio.Event,
    *,
    client: FootballClient | None = None,
    engine: AsyncEngine | None = None,
) -> None:
    owns_engine = engine is None
    owns_client = client is None
    engine = engine or make_async_engine(settings)
    session_factory = make_session_factory(engine)
    client = client or FootballClient(settings, locked=True)
    run_id: int | None = None
    try:
        async with session_factory() as session:
            async with session.begin():
                run_id = await record_run_start(session, settings, lock)
                await requeue_orphans(session)
                await ensure_cursors(session)
        scheduler = Scheduler(settings, client, session_factory)
        await scheduler.run(stop)
    finally:
        if run_id is not None:
            async with session_factory() as session:
                async with session.begin():
                    await record_run_end(session, run_id)
        if owns_client:
            await client.aclose()
        if owns_engine:
            await engine.dispose()


async def run_worker(
    settings: Settings | None = None,
    *,
    lock: WriterLock | None = None,
    stop: asyncio.Event | None = None,
    locked_loop: LockedLoop | None = None,
    client: FootballClient | None = None,
    engine: AsyncEngine | None = None,
) -> None:
    settings = settings or load_settings()
    configure_logging()
    log_startup(settings, service="worker")
    stop_event = stop if stop is not None else asyncio.Event()
    _install_stop_signals(stop_event)
    lock_obj = lock if lock is not None else WriterLock(settings)
    acquired = await lock_obj.try_acquire()
    if not acquired:
        info = await lock_obj.holder_info()
        await lock_obj.close()
        log_json(logging.WARNING, service="worker", event="lock_busy")
        raise LockBusyError(lock_busy_message(info))
    log_json(
        logging.INFO,
        service="worker",
        event="lock_acquired",
        backend_pid=lock_obj.backend_pid,
    )
    try:
        if locked_loop is not None:
            await locked_loop(settings, lock_obj, stop_event)
        else:
            await run_locked_loop(
                settings,
                lock_obj,
                stop_event,
                client=client,
                engine=engine,
            )
    except AuthBlockedError as exc:
        log_json(logging.ERROR, service="worker", event="auth_blocked")
        raise SystemExit(1) from exc
    finally:
        await lock_obj.release()
        await lock_obj.close()
        log_json(logging.INFO, service="worker", event="lock_released")


def main() -> None:
    configure_event_loop()
    try:
        asyncio.run(run_worker())
    except SettingsError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    except LockBusyError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
