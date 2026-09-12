"""Private status HTTP process. Does not call API-Football or take the writer lock."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI

from predictor.logutil import configure_logging, log_startup
from predictor.schemas.settings import Settings, SettingsError, load_settings


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = cast(Settings, app.state.settings)
    configure_logging()
    log_startup(settings, service="status")
    yield


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

    return app
