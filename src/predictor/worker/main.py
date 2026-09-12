"""Worker process. W0: validate config, ping Postgres, idle. No API, no writer lock."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from predictor.logutil import configure_logging, log_json, log_startup
from predictor.postgres import ping_postgres
from predictor.schemas.settings import SettingsError, load_settings


async def run_worker() -> None:
    settings = load_settings()
    configure_logging()
    log_startup(settings, service="worker")
    ping_postgres(settings)
    log_json(logging.INFO, service="worker", event="postgres_ok")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            break
    await stop.wait()


def main() -> None:
    try:
        asyncio.run(run_worker())
    except SettingsError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
