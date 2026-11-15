"""Read-only recommendation pages and token-gated posts. GET does not settle."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from predictor.api.errors import postgres_unavailable
from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorCycle,
    AdvisorGraphCheckpoint,
    AdvisorLeg,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.postgres import make_session_factory
from predictor.services.advisor.browse import (
    BrowseError,
    get_row,
    list_rows,
    table_counts,
)
from predictor.services.advisor.cycle import resume_waiting
from predictor.services.advisor.played import (
    load_played_coupons,
    parse_played_kind,
    parse_played_limit,
    played_coupons_as_dict,
)
from predictor.services.advisor.recommend import load_recommendation_setup
from predictor.services.advisor.recommend_ops import (
    ActionError,
    accept_coupon,
    launch_proposed,
    manual_settle,
    mark_not_placed,
    modify_coupon,
    place_coupon,
    promote_strategy,
    reject_coupon,
    set_strategy_status,
)
from predictor.services.metrics import mutation_token_ok
from predictor.ui.advisor import render_played_coupons
from predictor.ui.advisor_browse import (
    render_browse_index,
    render_browse_list,
    render_browse_row,
)
from predictor.ui.recommendations import (
    render_recommendation_card,
    render_recommendations,
    render_strategies,
    render_waiting_card,
)

_BROWSE_CODES = {
    "unknown_table": 404,
    "missing": 404,
    "bad_filter": 400,
    "bad_limit": 400,
}

_CODES = {
    "missing": 404,
    "not_manual": 404,
    "expired": 409,
    "not_proposed": 409,
    "closed": 409,
    "not_confirmable": 409,
    "not_recommendation": 409,
    "retired": 409,
    "status": 409,
    "strategy_limit": 409,
    "sample": 409,
    "yield": 409,
    "baseline": 409,
    "drawdown": 409,
    "stake": 400,
    "odd": 400,
    "empty": 400,
    "outcome": 400,
}


async def load_recommendation_html(engine: AsyncEngine) -> str:
    """Cards for the live board. Read-only."""
    factory = make_session_factory(engine)
    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    AdvisorCoupon.id,
                    AdvisorStrategy.name,
                    AdvisorCoupon.status,
                    AdvisorCoupon.combined_odd,
                )
                .join(
                    AdvisorStrategyVersion,
                    AdvisorStrategyVersion.id == AdvisorCoupon.strategy_version_id,
                )
                .join(
                    AdvisorStrategy,
                    AdvisorStrategy.id == AdvisorStrategyVersion.strategy_id,
                )
                .where(AdvisorCoupon.mode == "recommendation")
                .where(AdvisorCoupon.status.in_(("proposed", "accepted", "modified")))
                .order_by(AdvisorCoupon.id.desc())
                .limit(5)
            )
        ).all()
    if not rows:
        return ""
    view = tuple((int(row[0]), str(row[1]), str(row[2]), _odd(row[3])) for row in rows)
    return render_recommendations(view)


def _auth(request: Request, authorization: str | None, form_token: str | None) -> None:
    token = cast(str, request.app.state.mutation_token)
    if not mutation_token_ok(authorization, form_token, token):
        raise HTTPException(status_code=401, detail="unauthorized")


async def played_coupons(
    request: Request, kind: str = "all", limit: int = 50
) -> HTMLResponse:
    coupons = await _played(request.app, kind=kind, limit=limit)
    return HTMLResponse(render_played_coupons(coupons, kind=kind, limit=limit))


async def played_coupons_json(
    request: Request, kind: str = "all", limit: int = 50
) -> dict[str, Any]:
    coupons = await _played(request.app, kind=kind, limit=limit)
    return played_coupons_as_dict(coupons)


async def browse_index(request: Request) -> HTMLResponse:
    factory = make_session_factory(_engine(request))
    try:
        async with factory() as session:
            counts = await table_counts(session)
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    return HTMLResponse(render_browse_index(counts))


async def browse_index_json(request: Request) -> list[dict[str, Any]]:
    factory = make_session_factory(_engine(request))
    try:
        async with factory() as session:
            return await table_counts(session)
    except Exception as exc:
        raise postgres_unavailable(exc) from exc


async def browse_table_json(table: str, request: Request) -> JSONResponse:
    return JSONResponse(await _browse_list(request.app, table, request))


async def browse_row_json(request: Request, table: str, row_id: int) -> JSONResponse:
    return JSONResponse(await _browse_row(request.app, table, row_id))


async def browse_table(table: str, request: Request) -> HTMLResponse:
    payload = await _browse_list(request.app, table, request)
    return HTMLResponse(
        render_browse_list(
            table=payload["table"],
            rows=payload["rows"],
            filters=payload["filters"],
            limit=payload["limit"],
            next_before_id=payload["next_before_id"],
        )
    )


async def browse_row(request: Request, table: str, row_id: int) -> HTMLResponse:
    payload = await _browse_row(request.app, table, row_id)
    return HTMLResponse(render_browse_row(table=payload["table"], row=payload["row"]))


async def recommendations(request: Request) -> HTMLResponse:
    cycle_text = request.query_params.get("cycle")
    if cycle_text is not None:
        try:
            cycle_id = int(cycle_text)
        except ValueError:
            raise HTTPException(status_code=400, detail="bad_cycle") from None
        return await _waiting_page(request, cycle_id)
    factory = make_session_factory(_engine(request))
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(
                        AdvisorCoupon.id,
                        AdvisorStrategy.name,
                        AdvisorCoupon.status,
                        AdvisorCoupon.combined_odd,
                    )
                    .join(
                        AdvisorStrategyVersion,
                        AdvisorStrategyVersion.id == AdvisorCoupon.strategy_version_id,
                    )
                    .join(
                        AdvisorStrategy,
                        AdvisorStrategy.id == AdvisorStrategyVersion.strategy_id,
                    )
                    .where(AdvisorCoupon.mode == "recommendation")
                    .order_by(AdvisorCoupon.id.desc())
                    .limit(50)
                )
            ).all()
            waiting_rows = (
                await session.execute(
                    select(AdvisorGraphCheckpoint.cycle_id, AdvisorStrategy.name)
                    .join(
                        AdvisorCycle,
                        AdvisorCycle.id == AdvisorGraphCheckpoint.cycle_id,
                    )
                    .join(
                        AdvisorStrategyVersion,
                        AdvisorStrategyVersion.id == AdvisorCycle.strategy_version_id,
                    )
                    .join(
                        AdvisorStrategy,
                        AdvisorStrategy.id == AdvisorStrategyVersion.strategy_id,
                    )
                    .where(AdvisorGraphCheckpoint.status == "waiting_human")
                    .order_by(AdvisorGraphCheckpoint.cycle_id.desc())
                )
            ).all()
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    view = tuple((int(row[0]), str(row[1]), str(row[2]), _odd(row[3])) for row in rows)
    waiting = tuple((int(row[0]), str(row[1])) for row in waiting_rows)
    return HTMLResponse(render_recommendations(view, waiting))


async def recommendation_card(request: Request, coupon_id: int) -> HTMLResponse:
    factory = make_session_factory(_engine(request))
    try:
        async with factory() as session:
            card = await _card(session, coupon_id)
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    if card is None:
        raise HTTPException(status_code=404, detail="missing")
    return HTMLResponse(card)


async def accept(
    request: Request,
    coupon_id: int,
    token_field: str | None = Form(default=None, alias="token"),
    authorization: str | None = Header(default=None),
) -> RedirectResponse:
    _auth(request, authorization, token_field)
    await _act(request.app, lambda session: accept_coupon(session, coupon_id, _now()))
    return RedirectResponse(f"/advisor/recommendations/{coupon_id}", status_code=303)


async def reject(
    request: Request,
    coupon_id: int,
    token_field: str | None = Form(default=None, alias="token"),
    authorization: str | None = Header(default=None),
) -> RedirectResponse:
    _auth(request, authorization, token_field)
    await _act(
        request.app, lambda session: reject_coupon(session, coupon_id, _now(), "")
    )
    return RedirectResponse(f"/advisor/recommendations/{coupon_id}", status_code=303)


async def accept_cycle(
    request: Request,
    cycle_id: int,
    token_field: str | None = Form(default=None, alias="token"),
    authorization: str | None = Header(default=None),
) -> RedirectResponse:
    _auth(request, authorization, token_field)
    result = await _act(
        request.app,
        lambda session: resume_waiting(
            session,
            cycle_id,
            "accept",
            now=_now(),
            setup=load_recommendation_setup(),
        ),
    )
    if result == "accepted":
        coupon_id = await _cycle_coupon(request, cycle_id)
        if coupon_id is not None:
            return RedirectResponse(
                f"/advisor/recommendations/{coupon_id}", status_code=303
            )
    notice = result if isinstance(result, str) else "missing"
    return RedirectResponse(
        f"/advisor/recommendations?cycle={cycle_id}&notice={notice}",
        status_code=303,
    )


async def reject_cycle(
    request: Request,
    cycle_id: int,
    token_field: str | None = Form(default=None, alias="token"),
    authorization: str | None = Header(default=None),
) -> RedirectResponse:
    _auth(request, authorization, token_field)
    await _act(
        request.app,
        lambda session: resume_waiting(
            session,
            cycle_id,
            "reject",
            now=_now(),
            setup=load_recommendation_setup(),
        ),
    )
    return RedirectResponse(
        f"/advisor/recommendations?cycle={cycle_id}&notice=rejected",
        status_code=303,
    )


async def place(
    request: Request,
    coupon_id: int,
    token_field: str | None = Form(default=None, alias="token"),
    authorization: str | None = Header(default=None),
) -> RedirectResponse:
    _auth(request, authorization, token_field)
    await _act(request.app, lambda session: place_coupon(session, coupon_id, _now()))
    return RedirectResponse(f"/advisor/recommendations/{coupon_id}", status_code=303)


async def not_placed(
    request: Request,
    coupon_id: int,
    token_field: str | None = Form(default=None, alias="token"),
    authorization: str | None = Header(default=None),
) -> RedirectResponse:
    _auth(request, authorization, token_field)
    await _act(request.app, lambda session: mark_not_placed(session, coupon_id, _now()))
    return RedirectResponse(f"/advisor/recommendations/{coupon_id}", status_code=303)


async def modify(
    request: Request,
    coupon_id: int,
    stake: str = Form(default=""),
    odds: str = Form(default=""),
    remove: str = Form(default=""),
    token_field: str | None = Form(default=None, alias="token"),
    authorization: str | None = Header(default=None),
) -> RedirectResponse:
    _auth(request, authorization, token_field)
    try:
        stake_value = Decimal(stake)
        odd_map = _odds(odds)
        removed = _ids(remove)
    except ValueError, ArithmeticError:
        raise HTTPException(status_code=400, detail="invalid_form") from None
    await _act(
        request.app,
        lambda session: modify_coupon(
            session,
            coupon_id,
            _now(),
            stake=stake_value,
            odds=odd_map,
            remove=removed,
        ),
    )
    return RedirectResponse(f"/advisor/recommendations/{coupon_id}", status_code=303)


async def strategies(request: Request) -> HTMLResponse:
    factory = make_session_factory(_engine(request))
    try:
        async with factory() as session:
            rows = (
                await session.scalars(
                    select(AdvisorStrategy)
                    .where(AdvisorStrategy.kind == "llm")
                    .order_by(AdvisorStrategy.id)
                )
            ).all()
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    view = tuple((int(row.id), row.name, row.status, "") for row in rows)
    return HTMLResponse(render_strategies(view))


async def promote(
    request: Request,
    strategy_id: int,
    token_field: str | None = Form(default=None, alias="token"),
    authorization: str | None = Header(default=None),
) -> RedirectResponse:
    _auth(request, authorization, token_field)
    await _act(request.app, lambda session: promote_strategy(session, strategy_id))
    return RedirectResponse("/advisor/strategies", status_code=303)


async def pause(
    request: Request,
    strategy_id: int,
    token_field: str | None = Form(default=None, alias="token"),
    authorization: str | None = Header(default=None),
) -> RedirectResponse:
    _auth(request, authorization, token_field)
    await _act(
        request.app,
        lambda session: set_strategy_status(session, strategy_id, "paused"),
    )
    return RedirectResponse("/advisor/strategies", status_code=303)


async def resume(
    request: Request,
    strategy_id: int,
    token_field: str | None = Form(default=None, alias="token"),
    authorization: str | None = Header(default=None),
) -> RedirectResponse:
    _auth(request, authorization, token_field)
    await _act(
        request.app,
        lambda session: set_strategy_status(session, strategy_id, "sandbox"),
    )
    return RedirectResponse("/advisor/strategies", status_code=303)


async def launch(
    request: Request,
    strategy_id: int,
    token_field: str | None = Form(default=None, alias="token"),
    authorization: str | None = Header(default=None),
) -> RedirectResponse:
    _auth(request, authorization, token_field)
    await _act(
        request.app, lambda session: launch_proposed(session, strategy_id, limit=4)
    )
    return RedirectResponse("/advisor/strategies", status_code=303)


async def settle_leg(
    request: Request,
    leg_id: int,
    outcome: str = Form(default=""),
    token_field: str | None = Form(default=None, alias="token"),
    authorization: str | None = Header(default=None),
) -> RedirectResponse:
    _auth(request, authorization, token_field)
    await _act(request.app, lambda session: manual_settle(session, leg_id, outcome))
    return RedirectResponse("/advisor/recommendations", status_code=303)


def mount_advisor_http(app: FastAPI, token: str) -> None:
    """Register advisor routes. Each handler is its own function."""
    app.state.mutation_token = token
    app.get("/advisor/coupons")(played_coupons)
    app.get("/advisor/coupons.json")(played_coupons_json)
    app.get("/advisor/browse")(browse_index)
    app.get("/advisor/browse.json")(browse_index_json)
    app.get("/advisor/browse.json/{table}")(browse_table_json)
    app.get("/advisor/browse.json/{table}/{row_id}")(browse_row_json)
    app.get("/advisor/browse/{table}")(browse_table)
    app.get("/advisor/browse/{table}/{row_id}")(browse_row)
    app.get("/advisor/recommendations")(recommendations)
    app.post("/advisor/recommendations/cycles/{cycle_id}/accept")(accept_cycle)
    app.post("/advisor/recommendations/cycles/{cycle_id}/reject")(reject_cycle)
    app.get("/advisor/recommendations/{coupon_id}")(recommendation_card)
    app.post("/advisor/recommendations/{coupon_id}/accept")(accept)
    app.post("/advisor/recommendations/{coupon_id}/reject")(reject)
    app.post("/advisor/recommendations/{coupon_id}/place")(place)
    app.post("/advisor/recommendations/{coupon_id}/not-placed")(not_placed)
    app.post("/advisor/recommendations/{coupon_id}/modify")(modify)
    app.get("/advisor/strategies")(strategies)
    app.post("/advisor/strategies/{strategy_id}/promote")(promote)
    app.post("/advisor/strategies/{strategy_id}/pause")(pause)
    app.post("/advisor/strategies/{strategy_id}/resume")(resume)
    app.post("/advisor/strategies/{strategy_id}/launch")(launch)
    app.post("/advisor/legs/{leg_id}/settle")(settle_leg)


def _engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


async def _browse_list(app: FastAPI, table: str, request: Request) -> dict[str, Any]:
    reserved = frozenset({"limit", "before_id"})
    filters = {
        key: value
        for key, value in request.query_params.multi_items()
        if key not in reserved
    }
    limit_raw = request.query_params.get("limit")
    before_raw = request.query_params.get("before_id")
    try:
        limit = int(limit_raw) if limit_raw is not None else None
        before_id = int(before_raw) if before_raw is not None else None
    except ValueError:
        raise HTTPException(status_code=400, detail="bad_limit") from None
    engine = cast(AsyncEngine, app.state.engine)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            return await list_rows(
                session,
                table,
                filters=filters,
                before_id=before_id,
                limit=limit,
            )
    except BrowseError as exc:
        raise HTTPException(
            status_code=_BROWSE_CODES.get(exc.code, 400), detail=exc.code
        ) from None
    except Exception as exc:
        raise postgres_unavailable(exc) from exc


async def _browse_row(app: FastAPI, table: str, row_id: int) -> dict[str, Any]:
    engine = cast(AsyncEngine, app.state.engine)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            return await get_row(session, table, row_id)
    except BrowseError as exc:
        raise HTTPException(
            status_code=_BROWSE_CODES.get(exc.code, 400), detail=exc.code
        ) from None
    except Exception as exc:
        raise postgres_unavailable(exc) from exc


def _now() -> datetime:
    return datetime.now(UTC)


def _odd(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return format(value, "f")


def _odds(raw: str) -> dict[int, Decimal]:
    found: dict[int, Decimal] = {}
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        fixture_text, odd_text = piece.split("=", 1)
        found[int(fixture_text)] = Decimal(odd_text)
    return found


def _ids(raw: str) -> set[int]:
    return {int(part) for part in raw.split(",") if part.strip()}


async def _played(app: FastAPI, *, kind: str, limit: int) -> tuple[Any, ...]:
    try:
        parsed_kind = parse_played_kind(kind)
        parsed_limit = parse_played_limit(limit)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_parameter") from None
    engine = cast(AsyncEngine, app.state.engine)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            return await load_played_coupons(
                session, kind=parsed_kind, limit=parsed_limit
            )
    except Exception as exc:
        raise postgres_unavailable(exc) from exc


async def _act(
    app: FastAPI, call: Callable[[AsyncSession], Awaitable[object]]
) -> object:
    engine = cast(AsyncEngine, app.state.engine)
    factory = make_session_factory(engine)
    error: ActionError | None = None
    result: object = None
    try:
        async with factory() as session, session.begin():
            try:
                result = await call(session)
            except ActionError as exc:
                if exc.commit:
                    error = exc
                else:
                    raise
    except ActionError as exc:
        error = exc
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    if error is not None:
        raise HTTPException(status_code=_CODES.get(error.code, 409), detail=error.code)
    return result


_NOTICES = {
    "limited": "Limity rekomendacji nie przepuściły zapisu. Czekanie trwa.",
    "closed": "Czekanie się skończyło. Kupon nie powstał.",
    "rejected": "Odrzucone. Kupon nie powstał.",
    "conflict": "Mecz jest już na otwartej rekomendacji. Kupon nie powstał.",
    "missing": "Brak czekania na ten cykl.",
    "accepted": "Kupon rekomendacji nie powstał.",
}


async def _waiting_page(request: Request, cycle_id: int) -> HTMLResponse:
    factory = make_session_factory(_engine(request))
    try:
        async with factory() as session:
            row = await session.get(AdvisorGraphCheckpoint, cycle_id)
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="missing")
    lines = _waiting_lines(row.state)
    notice = _NOTICES.get(request.query_params.get("notice", ""), "")
    return HTMLResponse(
        render_waiting_card(
            cycle_id=cycle_id,
            status=row.status,
            lines=lines,
            notice=notice,
        )
    )


def _waiting_lines(state: object) -> tuple[str, ...]:
    if not isinstance(state, dict):
        return ()
    coupons = state.get("coupons")
    if not isinstance(coupons, list):
        return ()
    lines: list[str] = []
    for coupon in coupons:
        if not isinstance(coupon, list):
            continue
        for leg in coupon:
            if not isinstance(leg, dict):
                continue
            lines.append(
                f"mecz {leg.get('fixture_id')} {leg.get('period')} "
                f"{leg.get('goals_home')}:{leg.get('goals_away')} "
                f"Over {leg.get('n')}+0.5 kurs {leg.get('odd')}"
            )
    return tuple(lines)


async def _cycle_coupon(request: Request, cycle_id: int) -> int | None:
    factory = make_session_factory(_engine(request))
    try:
        async with factory() as session:
            coupon_id = await session.scalar(
                select(AdvisorCoupon.id)
                .where(AdvisorCoupon.cycle_id == cycle_id)
                .where(AdvisorCoupon.mode == "recommendation")
                .order_by(AdvisorCoupon.id.desc())
                .limit(1)
            )
    except Exception as exc:
        raise postgres_unavailable(exc) from exc
    if coupon_id is None:
        return None
    return int(coupon_id)


async def _card(session: AsyncSession, coupon_id: int) -> str | None:
    coupon = await session.get(AdvisorCoupon, coupon_id)
    if coupon is None or coupon.mode != "recommendation":
        return None
    version = await session.get(AdvisorStrategyVersion, coupon.strategy_version_id)
    floor = version.min_combined_odd if version is not None else Decimal("1.600")
    legs = (
        await session.scalars(
            select(AdvisorLeg).where(AdvisorLeg.coupon_id == coupon.id)
        )
    ).all()
    lines = tuple(
        (
            f"mecz {leg.fixture_id} {leg.period} {leg.goals_home}:{leg.goals_away} "
            f"Over {leg.n}+0.5 kurs {leg.odd} ({leg.status})"
        )
        for leg in legs
    )
    combined = coupon.combined_odd or Decimal("0")
    return render_recommendation_card(
        coupon_id=coupon.id,
        status=coupon.status,
        stake=format(coupon.stake, "f"),
        combined=format(combined, "f"),
        floor=format(floor, "f"),
        lines=lines,
        below=coupon.combined_odd is not None and coupon.combined_odd < floor,
    )
