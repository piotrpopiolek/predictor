"""Read-only recommendation pages and token-gated posts. GET does not settle."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from fastapi import FastAPI, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorLeg,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.postgres import make_session_factory
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
from predictor.ui.recommendations import (
    render_recommendation_card,
    render_recommendations,
    render_strategies,
)

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


def mount_advisor_http(app: FastAPI, token: str) -> None:
    def _auth(authorization: str | None, form_token: str | None) -> None:
        if not mutation_token_ok(authorization, form_token, token):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/advisor/recommendations")
    async def recommendations() -> HTMLResponse:
        engine = cast(AsyncEngine, app.state.engine)
        factory = make_session_factory(engine)
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
                            AdvisorStrategyVersion.id
                            == AdvisorCoupon.strategy_version_id,
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
        except Exception:
            raise HTTPException(
                status_code=503, detail="postgres_unavailable"
            ) from None
        view = tuple(
            (int(row[0]), str(row[1]), str(row[2]), _odd(row[3])) for row in rows
        )
        return HTMLResponse(render_recommendations(view))

    @app.get("/advisor/recommendations/{coupon_id}")
    async def recommendation_card(coupon_id: int) -> HTMLResponse:
        engine = cast(AsyncEngine, app.state.engine)
        factory = make_session_factory(engine)
        try:
            async with factory() as session:
                card = await _card(session, coupon_id)
        except Exception:
            raise HTTPException(
                status_code=503, detail="postgres_unavailable"
            ) from None
        if card is None:
            raise HTTPException(status_code=404, detail="missing")
        return HTMLResponse(card)

    @app.post("/advisor/recommendations/{coupon_id}/accept")
    async def accept(
        coupon_id: int,
        token_field: str | None = Form(default=None, alias="token"),
        authorization: str | None = Header(default=None),
    ) -> RedirectResponse:
        _auth(authorization, token_field)
        await _act(app, lambda session: accept_coupon(session, coupon_id, _now()))
        return RedirectResponse(
            f"/advisor/recommendations/{coupon_id}", status_code=303
        )

    @app.post("/advisor/recommendations/{coupon_id}/reject")
    async def reject(
        coupon_id: int,
        token_field: str | None = Form(default=None, alias="token"),
        authorization: str | None = Header(default=None),
    ) -> RedirectResponse:
        _auth(authorization, token_field)
        await _act(app, lambda session: reject_coupon(session, coupon_id, _now(), ""))
        return RedirectResponse(
            f"/advisor/recommendations/{coupon_id}", status_code=303
        )

    @app.post("/advisor/recommendations/{coupon_id}/place")
    async def place(
        coupon_id: int,
        token_field: str | None = Form(default=None, alias="token"),
        authorization: str | None = Header(default=None),
    ) -> RedirectResponse:
        _auth(authorization, token_field)
        await _act(app, lambda session: place_coupon(session, coupon_id, _now()))
        return RedirectResponse(
            f"/advisor/recommendations/{coupon_id}", status_code=303
        )

    @app.post("/advisor/recommendations/{coupon_id}/not-placed")
    async def not_placed(
        coupon_id: int,
        token_field: str | None = Form(default=None, alias="token"),
        authorization: str | None = Header(default=None),
    ) -> RedirectResponse:
        _auth(authorization, token_field)
        await _act(app, lambda session: mark_not_placed(session, coupon_id, _now()))
        return RedirectResponse(
            f"/advisor/recommendations/{coupon_id}", status_code=303
        )

    @app.post("/advisor/recommendations/{coupon_id}/modify")
    async def modify(
        coupon_id: int,
        stake: str = Form(default=""),
        odds: str = Form(default=""),
        remove: str = Form(default=""),
        token_field: str | None = Form(default=None, alias="token"),
        authorization: str | None = Header(default=None),
    ) -> RedirectResponse:
        _auth(authorization, token_field)
        try:
            stake_value = Decimal(stake)
            odd_map = _odds(odds)
            removed = _ids(remove)
        except ValueError, ArithmeticError:
            raise HTTPException(status_code=400, detail="invalid_form") from None
        await _act(
            app,
            lambda session: modify_coupon(
                session,
                coupon_id,
                _now(),
                stake=stake_value,
                odds=odd_map,
                remove=removed,
            ),
        )
        return RedirectResponse(
            f"/advisor/recommendations/{coupon_id}", status_code=303
        )

    @app.get("/advisor/strategies")
    async def strategies() -> HTMLResponse:
        engine = cast(AsyncEngine, app.state.engine)
        factory = make_session_factory(engine)
        try:
            async with factory() as session:
                rows = (
                    await session.scalars(
                        select(AdvisorStrategy)
                        .where(AdvisorStrategy.kind == "llm")
                        .order_by(AdvisorStrategy.id)
                    )
                ).all()
        except Exception:
            raise HTTPException(
                status_code=503, detail="postgres_unavailable"
            ) from None
        view = tuple((int(row.id), row.name, row.status, "") for row in rows)
        return HTMLResponse(render_strategies(view))

    @app.post("/advisor/strategies/{strategy_id}/promote")
    async def promote(
        strategy_id: int,
        token_field: str | None = Form(default=None, alias="token"),
        authorization: str | None = Header(default=None),
    ) -> RedirectResponse:
        _auth(authorization, token_field)
        await _act(app, lambda session: promote_strategy(session, strategy_id))
        return RedirectResponse("/advisor/strategies", status_code=303)

    @app.post("/advisor/strategies/{strategy_id}/pause")
    async def pause(
        strategy_id: int,
        token_field: str | None = Form(default=None, alias="token"),
        authorization: str | None = Header(default=None),
    ) -> RedirectResponse:
        _auth(authorization, token_field)
        await _act(
            app, lambda session: set_strategy_status(session, strategy_id, "paused")
        )
        return RedirectResponse("/advisor/strategies", status_code=303)

    @app.post("/advisor/strategies/{strategy_id}/resume")
    async def resume(
        strategy_id: int,
        token_field: str | None = Form(default=None, alias="token"),
        authorization: str | None = Header(default=None),
    ) -> RedirectResponse:
        _auth(authorization, token_field)
        await _act(
            app, lambda session: set_strategy_status(session, strategy_id, "sandbox")
        )
        return RedirectResponse("/advisor/strategies", status_code=303)

    @app.post("/advisor/strategies/{strategy_id}/launch")
    async def launch(
        strategy_id: int,
        token_field: str | None = Form(default=None, alias="token"),
        authorization: str | None = Header(default=None),
    ) -> RedirectResponse:
        _auth(authorization, token_field)
        await _act(app, lambda session: launch_proposed(session, strategy_id, limit=4))
        return RedirectResponse("/advisor/strategies", status_code=303)

    @app.post("/advisor/legs/{leg_id}/settle")
    async def settle_leg(
        leg_id: int,
        outcome: str = Form(default=""),
        token_field: str | None = Form(default=None, alias="token"),
        authorization: str | None = Header(default=None),
    ) -> RedirectResponse:
        _auth(authorization, token_field)
        await _act(app, lambda session: manual_settle(session, leg_id, outcome))
        return RedirectResponse("/advisor/recommendations", status_code=303)


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


async def _act(app: FastAPI, call: Callable[[AsyncSession], Awaitable[object]]) -> None:
    engine = cast(AsyncEngine, app.state.engine)
    factory = make_session_factory(engine)
    error: ActionError | None = None
    try:
        async with factory() as session, session.begin():
            try:
                await call(session)
            except ActionError as exc:
                if exc.commit:
                    error = exc
                else:
                    raise
    except ActionError as exc:
        error = exc
    except Exception:
        raise HTTPException(status_code=503, detail="postgres_unavailable") from None
    if error is not None:
        raise HTTPException(status_code=_CODES.get(error.code, 409), detail=error.code)


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
