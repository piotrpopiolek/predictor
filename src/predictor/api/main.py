"""Private status HTTP process. Does not call API-Football or take the writer lock."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from predictor.logutil import configure_logging, log_startup
from predictor.postgres import make_async_engine
from predictor.schemas.settings import Settings, SettingsError, load_settings
from predictor.services.health import (
    inspect_ready,
    operator_status,
    scrape_gauges,
    task_counts,
)
from predictor.services.lock import fetch_holder_sqlalchemy
from predictor.services.metrics import metrics_token_ok, render_metrics
from predictor.services.quota import (
    live_poll_interval_gauge,
    seconds_until_utc_midnight,
)
from predictor.telemetry import (
    configure_telemetry,
    instrument_engine,
    instrument_fastapi,
)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = cast(Settings, app.state.settings)
    configure_logging()
    configure_telemetry(settings, service="predictor-status")
    log_startup(settings, service="status")
    engine = make_async_engine(settings)
    instrument_engine(engine)
    app.state.engine = engine
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    app = FastAPI(title="Predictor status", lifespan=_lifespan)
    app.state.settings = settings

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        engine = cast(AsyncEngine, app.state.engine)
        payload, ok = await inspect_ready(settings, engine)
        return JSONResponse(payload, status_code=200 if ok else 503)

    @app.get("/status")
    async def status() -> dict[str, Any]:
        engine = cast(AsyncEngine, app.state.engine)
        try:
            return await operator_status(settings, engine)
        except Exception:
            raise HTTPException(
                status_code=503, detail="postgres_unavailable"
            ) from None

    @app.get("/metrics")
    async def metrics(
        authorization: str | None = Header(default=None),
    ) -> PlainTextResponse:
        token = settings.prometheus_metrics_token.get_secret_value()
        if not metrics_token_ok(authorization, token):
            raise HTTPException(status_code=401, detail="unauthorized")
        engine = cast(AsyncEngine, app.state.engine)
        try:
            async with engine.connect() as conn:
                holder = await fetch_holder_sqlalchemy(conn)
                await conn.execute(text("SELECT 1"))
            counts = await task_counts(engine)
            gauges = await scrape_gauges(engine)
        except Exception:
            raise HTTPException(
                status_code=503, detail="postgres_unavailable"
            ) from None
        remaining = int(gauges["quota_remaining"])
        used = int(gauges["quota_used"])
        now = datetime.now(UTC)
        interval = live_poll_interval_gauge(
            remaining,
            used,
            settings.quota_daily_limit,
            settings.live_poll_target_seconds,
            now,
        )
        body = render_metrics(
            environment=settings.host_environment.value,
            lock_held=holder is not None,
            task_counts=counts,
            in_play_fixtures=int(gauges["in_play_fixtures"]),
            live_last_snapshot_unixtime=gauges["live_last_snapshot_unixtime"],
            live_snapshot_age_seconds=gauges["live_snapshot_age_seconds"],
            oldest_pending_age_seconds=gauges["oldest_pending_age_seconds"],
            quota_plan=settings.quota_daily_limit,
            quota_remaining=remaining,
            quota_used=used,
            quota_seconds_until_reset=seconds_until_utc_midnight(now),
            live_poll_interval_seconds=interval,
        )
        return PlainTextResponse(
            body, media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    instrument_fastapi(app)
    return app
