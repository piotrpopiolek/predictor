"""Private status HTTP process. Does not call API-Football or take the writer lock."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from predictor.logutil import configure_logging, log_startup
from predictor.postgres import make_async_engine
from predictor.schemas.settings import Settings, SettingsError, load_settings
from predictor.services.health import (
    inspect_ready,
    operator_status,
    scrape_gauges,
    task_counts,
    task_counts_by_endpoint,
)
from predictor.services.live_board import (
    LiveMatch,
    list_live_matches,
    list_next_goal_matches,
    render_bets_html,
    render_live_html,
)
from predictor.services.lock import fetch_holder_sqlalchemy
from predictor.services.metrics import metrics_token_ok, render_metrics
from predictor.services.operator_bets import (
    BetError,
    bet_as_dict,
    current_stake_label,
    load_history_payload,
    parse_odd,
    place_bet,
    settle_and_list_open,
    settle_bet_manual,
    settle_open_from_engine,
    settle_open_tickets,
)
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

    async def _live_matches() -> list[LiveMatch]:
        engine = cast(AsyncEngine, app.state.engine)
        try:
            await settle_open_from_engine(engine)
            return await list_live_matches(engine)
        except Exception:
            raise HTTPException(
                status_code=503, detail="postgres_unavailable"
            ) from None

    @app.get("/")
    @app.get("/live")
    async def live() -> HTMLResponse:
        matches = await _live_matches()
        html = render_live_html(
            matches,
            generated_at=datetime.now(UTC),
            active_nav="all",
        )
        return HTMLResponse(html)

    @app.get("/live.json")
    async def live_json() -> dict[str, Any]:
        matches = await _live_matches()
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "count": len(matches),
            "matches": [item.as_dict() for item in matches],
        }

    async def _next_goal_board() -> list[tuple[LiveMatch, tuple[str, ...]]]:
        engine = cast(AsyncEngine, app.state.engine)
        try:
            await settle_open_from_engine(engine)
            return await list_next_goal_matches(engine)
        except Exception:
            raise HTTPException(
                status_code=503, detail="postgres_unavailable"
            ) from None

    @app.get("/live/next-goal")
    async def live_next_goal() -> HTMLResponse:
        engine = cast(AsyncEngine, app.state.engine)
        selected = await _next_goal_board()
        matches = [match for match, _reasons in selected]
        try:
            open_bets = await settle_and_list_open(engine)
            stake = await current_stake_label(engine)
        except Exception:
            raise HTTPException(
                status_code=503, detail="postgres_unavailable"
            ) from None
        html = render_live_html(
            matches,
            generated_at=datetime.now(UTC),
            title="Następny gol",
            empty=(
                "Brak meczów, w których faworyt przegrywa "
                "lub musi odrabiać w dwumeczu."
            ),
            active_nav="next_goal",
            open_bets={fid: bet_as_dict(bet) for fid, bet in open_bets.items()},
            current_stake=stake,
        )
        return HTMLResponse(html)

    @app.get("/live/next-goal.json")
    async def live_next_goal_json() -> dict[str, Any]:
        engine = cast(AsyncEngine, app.state.engine)
        selected = await _next_goal_board()
        try:
            open_bets = await settle_and_list_open(engine)
            stake = await current_stake_label(engine)
        except Exception:
            raise HTTPException(
                status_code=503, detail="postgres_unavailable"
            ) from None
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "count": len(selected),
            "current_stake": stake,
            "matches": [
                {
                    **match.as_dict(),
                    "next_goal_reasons": list(reasons),
                    "open_bet": (
                        None
                        if match.fixture_id not in open_bets
                        else bet_as_dict(open_bets[match.fixture_id])
                    ),
                }
                for match, reasons in selected
            ],
        }

    @app.get("/live/bets")
    async def live_bets() -> HTMLResponse:
        engine = cast(AsyncEngine, app.state.engine)
        try:
            payload = await load_history_payload(engine)
        except Exception:
            raise HTTPException(
                status_code=503, detail="postgres_unavailable"
            ) from None
        html = render_bets_html(payload, generated_at=datetime.now(UTC))
        return HTMLResponse(html)

    @app.get("/live/bets.json")
    async def live_bets_json() -> dict[str, Any]:
        engine = cast(AsyncEngine, app.state.engine)
        try:
            return await load_history_payload(engine)
        except Exception:
            raise HTTPException(
                status_code=503, detail="postgres_unavailable"
            ) from None

    @app.post("/live/bets", response_model=None)
    async def place_live_bet(
        request: Request,
        fixture_id: int = Form(...),
        odd: str = Form(...),
    ) -> RedirectResponse | JSONResponse:
        engine = cast(AsyncEngine, app.state.engine)
        wants_json = "application/json" in (request.headers.get("accept") or "")
        try:
            odd_dec = parse_odd(odd)
            selected = await list_next_goal_matches(engine)
            match = next(
                (m for m, _ in selected if m.fixture_id == fixture_id),
                None,
            )
            if match is None:
                raise BetError(404, "fixture_not_on_next_goal_board")
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await settle_open_tickets(session)
                bet = await place_bet(session, match=match, odd=odd_dec)
                await session.commit()
                payload = bet_as_dict(bet)
        except BetError as exc:
            if wants_json:
                return JSONResponse({"detail": exc.detail}, status_code=exc.code)
            raise HTTPException(status_code=exc.code, detail=exc.detail) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail="postgres_unavailable") from exc
        if wants_json:
            return JSONResponse(payload, status_code=201)
        return RedirectResponse(url="/live/next-goal", status_code=303)

    @app.post("/live/bets/{bet_id}/settle", response_model=None)
    async def settle_live_bet(
        bet_id: int,
        request: Request,
        outcome: str = Form(...),
    ) -> RedirectResponse | JSONResponse:
        engine = cast(AsyncEngine, app.state.engine)
        wants_json = "application/json" in (request.headers.get("accept") or "")
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                bet = await settle_bet_manual(session, bet_id, outcome)
                await session.commit()
                payload = bet_as_dict(bet)
        except BetError as exc:
            if wants_json:
                return JSONResponse({"detail": exc.detail}, status_code=exc.code)
            raise HTTPException(status_code=exc.code, detail=exc.detail) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="postgres_unavailable") from exc
        if wants_json:
            return JSONResponse(payload)
        referer = request.headers.get("referer") or "/live/bets"
        return RedirectResponse(url=referer, status_code=303)

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
            queue_rows = await task_counts_by_endpoint(engine)
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
            queue_counts=tuple(queue_rows),
        )
        return PlainTextResponse(
            body, media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    instrument_fastapi(app)
    return app
