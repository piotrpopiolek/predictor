"""App-surface case. The service tests stay in integration."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.integration.services.advisor.test_played import (
    _STRATEGY_NAMES,
    _seed,
    _wipe,
)

from predictor.api.main import create_app
from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings


@pytest.mark.asyncio
async def test_played_coupons_http_is_read_only() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            await _wipe(session)
            await _seed(session)
        with TestClient(create_app()) as client:
            bad_kind = client.get("/advisor/coupons.json", params={"kind": "nope"})
            assert bad_kind.status_code == 400
            bad_limit = client.get("/advisor/coupons.json", params={"limit": 0})
            assert bad_limit.status_code == 400
            response = client.get("/advisor/coupons.json", params={"limit": 50})
            assert response.status_code == 200
            body = response.json()
            ours = [
                item
                for item in body["coupons"]
                if str(item["strategy"]).startswith("played-")
            ]
            assert {item["kind"] for item in ours} == {"llm", "baseline"}
            assert any(item["mode"] == "recommendation" for item in ours)
            assert ours[0]["kind"] == "llm"
            assert ours[1]["kind"] == "baseline"
            baseline = client.get(
                "/advisor/coupons.json", params={"kind": "baseline", "limit": 50}
            )
            assert baseline.status_code == 200
            base_ours = [
                item
                for item in baseline.json()["coupons"]
                if str(item["strategy"]).startswith("played-")
            ]
            assert base_ours and all(item["kind"] == "baseline" for item in base_ours)
            page = client.get("/advisor/coupons", params={"kind": "llm", "limit": 25})
            assert page.status_code == 200
            assert "Zagrane kupony" in page.text
            assert "kind=llm" in page.text or "LLM" in page.text
            assert "limit=25" in page.text
        async with factory() as session:
            still_open = (
                await session.scalars(
                    select(AdvisorCoupon.status).where(
                        AdvisorCoupon.strategy_version_id.in_(
                            select(AdvisorStrategyVersion.id).where(
                                AdvisorStrategyVersion.strategy_id.in_(
                                    select(AdvisorStrategy.id).where(
                                        AdvisorStrategy.name.in_(_STRATEGY_NAMES)
                                    )
                                )
                            )
                        )
                    )
                )
            ).all()
        assert "open" in set(still_open)
        assert "placed" in set(still_open)
        assert "proposed" in set(still_open)
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()
