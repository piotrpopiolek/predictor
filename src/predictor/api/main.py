"""Private status HTTP process. Does not call API-Football or take the writer lock."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from predictor.logutil import configure_logging, log_startup
from predictor.postgres import make_async_engine
from predictor.schemas.settings import Settings, SettingsError, load_settings
from predictor.services.health import inspect_ready, operator_status, task_counts
from predictor.services.lock import fetch_holder_sqlalchemy
from predictor.services.metrics import metrics_token_ok, render_metrics


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = cast(Settings, app.state.settings)
    configure_logging()
    log_startup(settings, service="status")
    engine = make_async_engine(settings)
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
            raise HTTPException(status_code=503, detail="postgres_unavailable") from None

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
        except Exception:
            raise HTTPException(status_code=503, detail="postgres_unavailable") from None
        body = render_metrics(
            environment=settings.host_environment.value,
            lock_held=holder is not None,
            task_counts=counts,
        )
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")

    return app
