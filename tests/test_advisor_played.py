"""Played coupon list: live progress preview, load order, read-only GET."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from predictor.api.main import create_app
from predictor.constants import PAYOUT_KEEP
from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorLeg,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.models.catalog import League
from predictor.models.fixtures import Fixture, Team
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings
from predictor.services.advisor.domain import LiveProgress
from predictor.services.advisor.played import (
    load_played_coupons,
    played_coupons_as_dict,
)
from predictor.ui.advisor import render_played_coupons

KEEP = PAYOUT_KEEP
BASE_IDS = {
    "league": 881_901,
    "home": 881_902,
    "away": 881_903,
    "fixture_old": 881_904,
    "fixture_new": 881_905,
    "fixture_placed": 881_906,
    "fixture_proposed": 881_907,
}
_STRATEGY_NAMES = ("played-baseline", "played-llm")


def _now() -> datetime:
    # Ahead of live sandbox rows so the newest-first window includes these.
    return datetime.now(UTC) + timedelta(hours=1)


def _ours(rows: list | tuple) -> list:
    return [item for item in rows if item.strategy.startswith("played-")]


@pytest.mark.asyncio
async def test_load_played_coupons_newest_first_mixed_kinds() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            await _wipe(session)
            await _seed(session)
        async with factory() as session:
            rows = await load_played_coupons(session, kind="all", limit=50)
            statuses = (
                await session.execute(
                    select(AdvisorCoupon.id, AdvisorCoupon.status).where(
                        AdvisorCoupon.id.in_([item.id for item in rows])
                    )
                )
            ).all()
        ours = _ours(rows)
        assert [item.kind for item in ours[:2]] == ["llm", "baseline"]
        assert ours[0].placed_at is not None
        assert ours[1].placed_at is not None
        assert ours[0].placed_at > ours[1].placed_at
        assert ours[0].progress is LiveProgress.HIT
        assert ours[1].progress is LiveProgress.OPEN
        our_ids = {item.id for item in ours}
        assert {status for cid, status in statuses if cid in our_ids} >= {"open"}
        payload = played_coupons_as_dict(ours)
        assert payload["coupons"][0]["kind"] == "llm"
        assert payload["coupons"][0]["stake"] == "21.00"
        html = render_played_coupons(ours, kind="all", limit=50)
        assert "Spełniony" in html
        assert "Jeszcze nie" in html
        assert "Zagrane kupony" in html
        assert 'class="nav"' in html
        assert 'class="league"' in html
        assert 'class="match"' in html
        assert 'class="headline"' in html
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


@pytest.mark.asyncio
async def test_load_filters_kind_and_recommendation_statuses() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            await _wipe(session)
            await _seed(session)
        async with factory() as session:
            baseline = _ours(
                await load_played_coupons(session, kind="baseline", limit=50)
            )
            llm = _ours(await load_played_coupons(session, kind="llm", limit=50))
            all_rows = _ours(await load_played_coupons(session, kind="all", limit=50))
        assert {item.kind for item in baseline} == {"baseline"}
        assert {item.kind for item in llm} == {"llm"}
        modes = {(item.mode, item.status) for item in all_rows}
        assert ("sandbox", "open") in modes
        assert ("recommendation", "placed") in modes
        assert ("recommendation", "proposed") not in modes
        assert any(item.mode == "recommendation" for item in llm)
        assert not any(item.mode == "recommendation" for item in baseline)
    finally:
        async with factory() as session, session.begin():
            await _wipe(session)
        await engine.dispose()


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


async def _seed(session) -> None:
    now = _now()
    session.add(League(id=BASE_IDS["league"], name="advisor-played"))
    session.add(Team(id=BASE_IDS["home"], name="Played Home"))
    session.add(Team(id=BASE_IDS["away"], name="Played Away"))
    await session.flush()
    for fixture_id, elapsed, goals_home, hours in (
        (BASE_IDS["fixture_old"], 70, 1, 3),
        (BASE_IDS["fixture_new"], 65, 2, 2),
        (BASE_IDS["fixture_placed"], 72, 2, 1),
        (BASE_IDS["fixture_proposed"], 68, 1, 1),
    ):
        session.add(
            Fixture(
                id=fixture_id,
                league_id=BASE_IDS["league"],
                season=2026,
                home_team_id=BASE_IDS["home"],
                away_team_id=BASE_IDS["away"],
                status_short="2H",
                elapsed_minutes=elapsed,
                goals_home=goals_home,
                goals_away=0,
                date=now - timedelta(hours=hours),
            )
        )
    await session.flush()
    baseline = AdvisorStrategy(
        name="played-baseline", kind="baseline", status="sandbox"
    )
    llm = AdvisorStrategy(name="played-llm", kind="llm", status="sandbox")
    session.add(baseline)
    session.add(llm)
    await session.flush()
    base_version = AdvisorStrategyVersion(
        strategy_id=baseline.id,
        version_no=1,
        prompt_version="p4",
        min_combined_odd=Decimal("1.600"),
        max_legs=3,
        max_minute_regulation=80,
        max_minute_extra=110,
        wait_minutes=10,
        candidate_rules=["favorite_losing"],
    )
    llm_version = AdvisorStrategyVersion(
        strategy_id=llm.id,
        version_no=1,
        prompt_version="p4",
        min_combined_odd=Decimal("1.600"),
        max_legs=3,
        max_minute_regulation=80,
        max_minute_extra=110,
        wait_minutes=10,
        candidate_rules=["favorite_losing"],
    )
    session.add(base_version)
    session.add(llm_version)
    await session.flush()
    older = AdvisorCoupon(
        mode="sandbox",
        strategy_version_id=base_version.id,
        status="open",
        stake=Decimal("21.00"),
        combined_odd=Decimal("1.700"),
        proposed_combined_odd=Decimal("1.700"),
        payout_keep=KEEP,
        placed_at=now - timedelta(minutes=3),
    )
    newer = AdvisorCoupon(
        mode="sandbox",
        strategy_version_id=llm_version.id,
        status="open",
        stake=Decimal("21.00"),
        combined_odd=Decimal("1.800"),
        proposed_combined_odd=Decimal("1.800"),
        payout_keep=KEEP,
        placed_at=now - timedelta(minutes=2),
    )
    placed = AdvisorCoupon(
        mode="recommendation",
        strategy_version_id=llm_version.id,
        status="placed",
        stake=Decimal("21.00"),
        combined_odd=Decimal("1.900"),
        proposed_combined_odd=Decimal("1.900"),
        payout_keep=KEEP,
        placed_at=now - timedelta(minutes=4),
    )
    proposed = AdvisorCoupon(
        mode="recommendation",
        strategy_version_id=llm_version.id,
        status="proposed",
        stake=Decimal("21.00"),
        combined_odd=Decimal("1.750"),
        proposed_combined_odd=Decimal("1.750"),
        payout_keep=KEEP,
        placed_at=None,
    )
    session.add(older)
    session.add(newer)
    session.add(placed)
    session.add(proposed)
    await session.flush()
    session.add(
        AdvisorLeg(
            coupon_id=older.id,
            strategy_id=baseline.id,
            fixture_id=BASE_IDS["fixture_old"],
            mode="sandbox",
            period="regulation",
            n=1,
            odd=Decimal("1.700"),
            proposed_odd=Decimal("1.700"),
            goals_home=1,
            goals_away=0,
            status="open",
        )
    )
    session.add(
        AdvisorLeg(
            coupon_id=newer.id,
            strategy_id=llm.id,
            fixture_id=BASE_IDS["fixture_new"],
            mode="sandbox",
            period="regulation",
            n=1,
            odd=Decimal("1.800"),
            proposed_odd=Decimal("1.800"),
            goals_home=1,
            goals_away=0,
            status="open",
        )
    )
    session.add(
        AdvisorLeg(
            coupon_id=placed.id,
            strategy_id=llm.id,
            fixture_id=BASE_IDS["fixture_placed"],
            mode="recommendation",
            period="regulation",
            n=1,
            odd=Decimal("1.900"),
            proposed_odd=Decimal("1.900"),
            goals_home=1,
            goals_away=0,
            status="open",
        )
    )
    session.add(
        AdvisorLeg(
            coupon_id=proposed.id,
            strategy_id=llm.id,
            fixture_id=BASE_IDS["fixture_proposed"],
            mode="recommendation",
            period="regulation",
            n=1,
            odd=Decimal("1.750"),
            proposed_odd=Decimal("1.750"),
            goals_home=1,
            goals_away=0,
            status="open",
        )
    )


async def _wipe(session) -> None:
    strategy_ids = select(AdvisorStrategy.id).where(
        AdvisorStrategy.name.in_(_STRATEGY_NAMES)
    )
    version_ids = select(AdvisorStrategyVersion.id).where(
        AdvisorStrategyVersion.strategy_id.in_(strategy_ids)
    )
    coupon_ids = select(AdvisorCoupon.id).where(
        AdvisorCoupon.strategy_version_id.in_(version_ids)
    )
    await session.execute(
        delete(AdvisorLeg).where(AdvisorLeg.strategy_id.in_(strategy_ids))
    )
    await session.execute(delete(AdvisorCoupon).where(AdvisorCoupon.id.in_(coupon_ids)))
    await session.execute(
        delete(AdvisorStrategyVersion).where(AdvisorStrategyVersion.id.in_(version_ids))
    )
    await session.execute(
        delete(AdvisorStrategy).where(AdvisorStrategy.id.in_(strategy_ids))
    )
    await session.execute(
        delete(Fixture).where(
            Fixture.id.in_(
                (
                    BASE_IDS["fixture_old"],
                    BASE_IDS["fixture_new"],
                    BASE_IDS["fixture_placed"],
                    BASE_IDS["fixture_proposed"],
                )
            )
        )
    )
    await session.execute(
        delete(Team).where(Team.id.in_((BASE_IDS["home"], BASE_IDS["away"])))
    )
    await session.execute(delete(League).where(League.id == BASE_IDS["league"]))
