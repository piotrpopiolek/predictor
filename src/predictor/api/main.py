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

from predictor.api.advisor_http import load_recommendation_html, mount_advisor_http
from predictor.api.errors import postgres_unavailable
from predictor.logutil import configure_logging, log_startup
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings, SettingsError, load_settings
from predictor.services.advisor.llm_note import (
    load_llm_notes,
    load_sandbox_coupon_labels,
)
from predictor.services.advisor.report import load_yield_report, report_as_dict
from predictor.services.health import (
    inspect_ready,
    operator_status,
    scrape_gauges,
    task_counts,
    task_counts_by_endpoint,
)
from predictor.services.live_board import (
    LiveMatch,
    list_day_matches,
    list_live_matches,
    list_next_goal_matches,
    parse_query_day,
)
from predictor.services.lock import fetch_holder_sqlalchemy
from predictor.services.match_detail import load_match_detail
from predictor.services.metrics import metrics_token_ok, render_metrics
from predictor.services.operator_bets import (
    BetError,
    bet_as_dict,
    current_stake_label,
    list_open_from_engine,
    load_history_payload,
    parse_odd,
    place_bet,
    settle_bet_manual,
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
from predictor.ui import (
    render_bets_html,
    render_day_html,
    render_live_html,
    render_match_html,
    render_match_missing,
)
from predictor.ui.advisor import render_yield_report
from predictor.ui.llm_note import render_llm_note

_REPORT_PERIODS = frozenset({"all", "regulation", "extra_time"})


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


def _engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def health() -> dict[str, str]:
    return {"status": "ok"}


async def ready(request: Request) -> JSONResponse:
    payload, ok = await inspect_ready(_settings(request), _engine(request))
    return JSONResponse(payload, status_code=200 if ok else 503)


async def status(request: Request) -> dict[str, Any]:
    try:
        return await operator_status(_settings(request), _engine(request))
    except Exception as exc:
        raise postgres_unavailable(exc) from exc


async def _live_matches(engine: AsyncEngine) -> list[LiveMatch]:
    try:
        return await list_live_matches(engine)
    except Exception as exc:
        raise postgres_unavailable(exc) from exc


async def live(request: Request) -> HTMLResponse:
    engine = _engine(request)
    matches = await _live_matches(engine)
    try:
        coupons = await load_sandbox_coupon_labels(
            engine, [item.fixture_id for item in matches]
        )
    except Exception:
        coupons = {}
    html = render_live_html(
        matches,
        generated_at=datetime.now(UTC),
        active_nav="all",
        sandbox_coupons=coupons,
    )
    return HTMLResponse(html)


async def live_json(request: Request) -> dict[str, Any]:
    engine = _engine(request)
    matches = await _live_matches(engine)
    try:
        coupons = await load_sandbox_coupon_labels(
            engine, [item.fixture_id for item in matches]
        )
    except Exception:
        coupons = {}
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "count": len(matches),
        "matches": [
            {
                **item.as_dict(),
                "sandbox_coupon": coupons.get(item.fixture_id),
            }
            for item in matches
        ],
    }


async def _next_goal_board(
    engine: AsyncEngine,
) -> list[tuple[LiveMatch, tuple[str, ...]]]:
    try:
        return await list_next_goal_matches(engine)
    except Exception as exc:
        raise postgres_unavailable(exc) from exc


async def live_next_goal(request: Request) -> HTMLResponse:
    engine = _engine(request)
    selected = await _next_goal_board(engine)
    matches = [match for match, _reasons in selected]
    try:
        open_bets = await list_open_from_engine(engine)
        stake = await current_stake_label(engine)
        notes = await load_llm_notes(
            engine,
            matches,
            candidate_ids={item.fixture_id for item in matches},
        )
        sandbox_coupons = await load_sandbox_coupon_labels(
            engine, [item.fixture_id for item in matches]
        )
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    try:
        cards = await load_recommendation_html(engine)
    except Exception:
        cards = ""
    html = render_live_html(
        matches,
        generated_at=datetime.now(UTC),
        title="Następny gol",
        recommendation_html=cards,
        empty=(
            "Brak meczów, w których faworyt przegrywa lub musi odrabiać w dwumeczu."
        ),
        active_nav="next_goal",
        open_bets={fid: bet_as_dict(bet) for fid, bet in open_bets.items()},
        current_stake=stake,
        llm_notes={
            fixture_id: render_llm_note(note) for fixture_id, note in notes.items()
        },
        sandbox_coupons=sandbox_coupons,
    )
    return HTMLResponse(html)


async def live_next_goal_json(request: Request) -> dict[str, Any]:
    engine = _engine(request)
    selected = await _next_goal_board(engine)
    matches = [match for match, _reasons in selected]
    try:
        open_bets = await list_open_from_engine(engine)
        stake = await current_stake_label(engine)
        notes = await load_llm_notes(
            engine,
            matches,
            candidate_ids={item.fixture_id for item in matches},
        )
        sandbox_coupons = await load_sandbox_coupon_labels(
            engine, [item.fixture_id for item in matches]
        )
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
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
                "llm": notes[match.fixture_id].as_dict(),
                "sandbox_coupon": sandbox_coupons.get(match.fixture_id),
                "llm_coupon": sandbox_coupons.get(match.fixture_id) == "Kupon LLM",
            }
            for match, reasons in selected
        ],
    }


async def live_day(request: Request, day: str | None = None) -> HTMLResponse:
    return await _day_page(request, day)


async def live_day_path(request: Request, day: str) -> HTMLResponse:
    return await _day_page(request, day)


async def _day_page(request: Request, raw: str | None) -> HTMLResponse:
    today = datetime.now(UTC).date()
    invalid = False
    if raw is None or not raw.strip():
        selected = today
    else:
        parsed = parse_query_day(raw)
        if parsed is None:
            invalid = True
            selected = today
        else:
            selected = parsed
    try:
        matches = await list_day_matches(_engine(request), selected)
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    html = render_day_html(
        matches,
        day=selected,
        today=today,
        generated_at=datetime.now(UTC),
        invalid_date=invalid,
    )
    return HTMLResponse(html)


async def live_match(request: Request, fixture_id: int) -> HTMLResponse:
    engine = _engine(request)
    try:
        detail = await load_match_detail(engine, fixture_id)
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    if detail is None:
        return HTMLResponse(render_match_missing(), status_code=404)
    try:
        notes = await load_llm_notes(engine, [detail.match])
        coupons = await load_sandbox_coupon_labels(engine, [detail.match.fixture_id])
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    note = notes.get(detail.match.fixture_id)
    llm_html = "" if note is None else render_llm_note(note)
    return HTMLResponse(
        render_match_html(
            detail,
            generated_at=datetime.now(UTC),
            llm_html=llm_html,
            sandbox_coupon=coupons.get(detail.match.fixture_id),
        )
    )


async def live_bets(request: Request) -> HTMLResponse:
    try:
        payload = await load_history_payload(_engine(request))
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    html = render_bets_html(
        payload,
        generated_at=datetime.now(UTC),
    )
    return HTMLResponse(html)


async def live_bets_json(request: Request) -> dict[str, Any]:
    try:
        return await load_history_payload(_engine(request))
    except Exception as exc:
        raise postgres_unavailable(exc) from exc


async def place_live_bet(
    request: Request,
    fixture_id: int = Form(...),
    odd: str = Form(...),
) -> RedirectResponse | JSONResponse:
    engine = _engine(request)
    wants_json = "application/json" in (request.headers.get("accept") or "")
    try:
        odd_dec = parse_odd(odd)
        selected = await list_next_goal_matches(engine)
        match = next(
            (item for item, _reasons in selected if item.fixture_id == fixture_id),
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
        raise postgres_unavailable(exc) from exc
    if wants_json:
        return JSONResponse(payload, status_code=201)
    return RedirectResponse(url="/live/next-goal", status_code=303)


async def settle_live_bet(
    bet_id: int,
    request: Request,
    outcome: str = Form(...),
) -> RedirectResponse | JSONResponse:
    wants_json = "application/json" in (request.headers.get("accept") or "")
    try:
        async with AsyncSession(_engine(request), expire_on_commit=False) as session:
            bet = await settle_bet_manual(session, bet_id, outcome)
            await session.commit()
            payload = bet_as_dict(bet)
    except BetError as exc:
        if wants_json:
            return JSONResponse({"detail": exc.detail}, status_code=exc.code)
        raise HTTPException(status_code=exc.code, detail=exc.detail) from exc
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    if wants_json:
        return JSONResponse(payload)
    referer = request.headers.get("referer") or "/live/bets"
    return RedirectResponse(url=referer, status_code=303)


async def metrics(
    request: Request,
    authorization: str | None = Header(default=None),
) -> PlainTextResponse:
    settings = _settings(request)
    token = settings.prometheus_metrics_token.get_secret_value()
    if not metrics_token_ok(authorization, token):
        raise HTTPException(status_code=401, detail="unauthorized")
    engine = _engine(request)
    try:
        async with engine.connect() as conn:
            holder = await fetch_holder_sqlalchemy(conn)
            await conn.execute(text("SELECT 1"))
        counts = await task_counts(engine)
        queue_rows = await task_counts_by_endpoint(engine)
        gauges = await scrape_gauges(engine)
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    remaining = int(gauges["quota_remaining"])
    used = int(gauges["quota_used"])
    vendor_plan = int(gauges.get("quota_plan") or 0)
    quota_plan = vendor_plan if vendor_plan > 0 else settings.quota_daily_limit
    now = datetime.now(UTC)
    planned = float(gauges.get("score_poll_seconds") or 0)
    interval = (
        planned
        if planned > 0
        else live_poll_interval_gauge(
            remaining,
            used,
            quota_plan,
            settings.live_poll_target_seconds,
            now,
        )
    )
    body = render_metrics(
        environment=settings.host_environment.value,
        lock_held=holder is not None,
        task_counts=counts,
        in_play_fixtures=int(gauges["in_play_fixtures"]),
        live_last_snapshot_unixtime=gauges["live_last_snapshot_unixtime"],
        live_snapshot_age_seconds=gauges["live_snapshot_age_seconds"],
        oldest_pending_age_seconds=gauges["oldest_pending_age_seconds"],
        quota_plan=quota_plan,
        quota_remaining=remaining,
        quota_used=used,
        quota_seconds_until_reset=seconds_until_utc_midnight(now),
        live_poll_interval_seconds=interval,
        detail_refresh_seconds=gauges.get("detail_refresh_seconds", 0.0),
        context_call_cap=gauges.get("context_call_cap", 0.0),
        quota_overloaded=gauges.get("quota_overloaded", 0.0),
        quota_allowed_per_minute=gauges.get("quota_allowed_per_minute", 0.0),
        queue_counts=tuple(queue_rows),
    )
    return PlainTextResponse(
        body, media_type="text/plain; version=0.0.4; charset=utf-8"
    )


async def advisor_report(request: Request, period: str = "all") -> HTMLResponse:
    if period not in _REPORT_PERIODS:
        raise HTTPException(status_code=400, detail="invalid_period")
    factory = make_session_factory(_engine(request))
    try:
        async with factory() as session:
            report = await load_yield_report(session, period=period)
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    return HTMLResponse(render_yield_report(report))


async def advisor_report_json(request: Request, period: str = "all") -> dict[str, Any]:
    if period not in _REPORT_PERIODS:
        raise HTTPException(status_code=400, detail="invalid_period")
    factory = make_session_factory(_engine(request))
    try:
        async with factory() as session:
            report = await load_yield_report(session, period=period)
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    return report_as_dict(report)


def mount_status_http(app: FastAPI) -> None:
    """Register status routes. Each handler is its own function."""
    app.get("/health")(health)
    app.get("/ready")(ready)
    app.get("/status")(status)
    app.get("/")(live)
    app.get("/live")(live)
    app.get("/live.json")(live_json)
    app.get("/live/next-goal")(live_next_goal)
    app.get("/live/next-goal.json")(live_next_goal_json)
    app.get("/live/day", response_model=None)(live_day)
    app.get("/live/day/{day}", response_model=None)(live_day_path)
    app.get("/live/match/{fixture_id}", response_model=None)(live_match)
    app.get("/live/bets")(live_bets)
    app.get("/live/bets.json")(live_bets_json)
    app.post("/live/bets", response_model=None)(place_live_bet)
    app.post("/live/bets/{bet_id}/settle", response_model=None)(settle_live_bet)
    app.get("/metrics")(metrics)
    app.get("/advisor/report")(advisor_report)
    app.get("/advisor/report.json")(advisor_report_json)


def create_app() -> FastAPI:
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    app = FastAPI(title="Predictor status", lifespan=_lifespan)
    app.state.settings = settings
    mount_status_http(app)
    mount_advisor_http(app)
    instrument_fastapi(app)
    return app
