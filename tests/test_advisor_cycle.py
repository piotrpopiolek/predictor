"""One recorded model call and the daily budget gate."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorCycle,
    AdvisorDecision,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.models.catalog import League
from predictor.models.fixtures import Fixture, Team
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.advisor import AdvisorSettings
from predictor.schemas.settings import Settings
from predictor.services.advisor.budget import RESERVED_OUTPUT_TOKENS
from predictor.services.advisor.cycle import (
    CycleCandidate,
    CycleRequest,
    run_cycle,
)
from predictor.services.advisor.llm.protocol import LlmCompletion, ProviderError

NOW = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)
LEAGUE = 880_001
HOME = 880_002
AWAY = 880_003
FIXTURE = 880_004


class _Provider:
    provider_name = "google"
    model = "gemini-2.5-flash"

    def __init__(
        self, text: str | None = None, error: ProviderError | None = None
    ) -> None:
        self.text = text
        self.error = error
        self.calls = 0
        self.open_during_call = False
        self.session = None

    async def decide(self, prompt: str, *, timeout: float) -> LlmCompletion:
        session = self.session
        if session is not None and session.in_transaction():
            self.open_during_call = True
        self.calls += 1
        if self.error is not None:
            raise self.error
        return LlmCompletion(
            text=self.text or "",
            provider=self.provider_name,
            model=self.model,
            model_version="gemini-2.5-flash-001",
            input_tokens=1_000_000,
            output_tokens=400_000,
            thoughts_tokens=100_000,
        )


def _settings() -> AdvisorSettings:
    return AdvisorSettings(
        llm_api_key="secret",
        llm_price_input_pln_per_mtok=Decimal("0.50"),
        llm_price_output_pln_per_mtok=Decimal("1.25"),
        advisor_daily_budget_pln=Decimal("10"),
    )


def _request() -> CycleRequest:
    return CycleRequest(
        strategy_id=0,
        strategy_version_id=0,
        mode="sandbox",
        prompt_version="p1",
        prompt="score 0-1",
        input_json={"fixture_id": FIXTURE},
        candidates=(
            CycleCandidate(
                fixture_id=FIXTURE,
                period="regulation",
                goals_home=0,
                goals_away=1,
                ordinal=2,
                candidate_rule="favorite_losing",
                odd=Decimal("1.70"),
                n=1,
            ),
        ),
        open_fixture_ids=frozenset(),
        min_odd=Decimal("1.6"),
        max_legs=3,
    )


def _reply(decision: str, fixture_id: int = FIXTURE) -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "fixture_id": fixture_id,
                    "decision": decision,
                    "estimated_probability": "0.60",
                    "confidence": "0.40",
                    "reasons": ["favorite_pressure"],
                    "rationale": "chasing the score",
                }
            ],
            "coupons": [{"fixture_ids": [fixture_id]}] if decision == "bet" else [],
        }
    )


@pytest.mark.asyncio
async def test_run_cycle_appends_a_valid_call_and_respects_the_budget() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    provider = _Provider(_reply("reject"))
    version_id = 0
    strategy_id = 0
    try:
        async with factory() as session:
            async with session.begin():
                version_id, strategy_id = await _seed(session)
            request = replace(
                _request(),
                strategy_id=strategy_id,
                strategy_version_id=version_id,
            )
            provider.session = session
            async with session.begin():
                with pytest.raises(RuntimeError, match="open transaction"):
                    await run_cycle(
                        session, provider, _settings(), request, clock=lambda: NOW
                    )
            assert provider.calls == 0

            outcome = await run_cycle(
                session, provider, _settings(), request, clock=lambda: NOW
            )
            assert provider.open_during_call is False
            assert outcome.called
            assert outcome.status == "ok"
            assert provider.calls == 1
            async with session.begin():
                cycle = await session.get(AdvisorCycle, outcome.cycle_id)
                assert cycle is not None
                assert cycle.model == "gemini-2.5-flash"
                assert cycle.model_version == "gemini-2.5-flash-001"
                assert cycle.thoughts_tokens == 100_000
                assert cycle.cost_pln == Decimal("1.1250")
                assert cycle.validation_status == "ok"
                decisions = (
                    await session.scalars(
                        select(AdvisorDecision).where(
                            AdvisorDecision.cycle_id == cycle.id
                        )
                    )
                ).all()
                assert len(decisions) == 1
                assert decisions[0].decision == "reject"
                coupons = await session.scalar(
                    select(func.count())
                    .select_from(AdvisorCoupon)
                    .where(AdvisorCoupon.cycle_id == cycle.id)
                )
                assert coupons == 0

            broken = _Provider("nope")
            broken.session = session
            invalid = await run_cycle(
                session, broken, _settings(), request, clock=lambda: NOW
            )
            assert broken.open_during_call is False
            assert invalid.status == "invalid_output"
            async with session.begin():
                invalid_rows = await session.scalar(
                    select(func.count())
                    .select_from(AdvisorDecision)
                    .where(AdvisorDecision.cycle_id == invalid.cycle_id)
                )
                assert invalid_rows == 0

            failed = _Provider(error=ProviderError("timeout"))
            failed.session = session
            provider_error = await run_cycle(
                session, failed, _settings(), request, clock=lambda: NOW
            )
            assert failed.open_during_call is False
            assert provider_error.status == "provider_error"
            assert provider_error.detail == "timeout"

            async with session.begin():
                session.add(
                    AdvisorCycle(
                        strategy_version_id=version_id,
                        mode="sandbox",
                        started_at=NOW,
                        provider="google",
                        model="gemini-2.5-flash",
                        model_version="gemini-2.5-flash",
                        prompt_version="p1",
                        prompt_text="seed",
                        input_json={},
                        validation_status="ok",
                        cost_pln=Decimal("7.0000"),
                        thoughts_tokens=0,
                    )
                )
            blocked = _Provider(_reply("bet"))
            blocked.session = session
            skipped = await run_cycle(
                session, blocked, _settings(), request, clock=lambda: NOW
            )
            assert skipped.called is False
            assert skipped.status == "budget"
            assert blocked.calls == 0
            recommend = replace(request, mode="recommendation")
            allowed = await run_cycle(
                session,
                blocked,
                _settings(),
                recommend,
                clock=lambda: NOW,
            )
            assert blocked.open_during_call is False
            assert allowed.called
            assert allowed.status == "ok"
    finally:
        async with factory() as session, session.begin():
            await _cleanup(session, strategy_id, version_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_second_cycle_waits_until_the_first_cost_is_committed() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    version_id = 0
    strategy_id = 0
    release = asyncio.Event()
    entered = asyncio.Event()
    settings = AdvisorSettings(
        llm_api_key="secret",
        llm_price_input_pln_per_mtok=Decimal("0.01"),
        llm_price_output_pln_per_mtok=Decimal("2000"),
        advisor_daily_budget_pln=Decimal("10"),
    )

    class _Slow(_Provider):
        async def decide(self, prompt: str, *, timeout: float) -> LlmCompletion:
            self.calls += 1
            entered.set()
            await release.wait()
            return LlmCompletion(
                text=self.text or "",
                provider=self.provider_name,
                model=self.model,
                model_version="gemini-2.5-flash-001",
                input_tokens=2,
                output_tokens=RESERVED_OUTPUT_TOKENS,
                thoughts_tokens=0,
            )

    try:
        async with factory() as session:
            async with session.begin():
                version_id, strategy_id = await _seed(
                    session,
                    league=880_011,
                    home=880_012,
                    away=880_013,
                    fixture=880_014,
                    name="advisor-cycle-race",
                )
            request = replace(
                _request(),
                strategy_id=strategy_id,
                strategy_version_id=version_id,
                mode="sandbox",
                candidates=(replace(_request().candidates[0], fixture_id=880_014),),
            )
        async with factory() as left, factory() as right:
            first = _Slow(_reply("reject", 880_014))
            second = _Provider(_reply("reject", 880_014))
            lead = asyncio.create_task(
                run_cycle(left, first, settings, request, clock=lambda: NOW)
            )
            await entered.wait()
            follow = asyncio.create_task(
                run_cycle(right, second, settings, request, clock=lambda: NOW)
            )
            await asyncio.sleep(0.2)
            assert not follow.done()
            release.set()
            lead_outcome = await lead
            follow_outcome = await follow
            assert lead_outcome.status == "ok"
            assert follow_outcome.status == "budget"
            assert second.calls == 0
            async with left.begin():
                spent = await left.scalar(
                    select(func.coalesce(func.sum(AdvisorCycle.cost_pln), 0)).where(
                        AdvisorCycle.strategy_version_id == version_id
                    )
                )
            assert spent == Decimal("4.0960")
            assert spent <= Decimal("7")
    finally:
        async with factory() as session, session.begin():
            await _cleanup(
                session,
                strategy_id,
                version_id,
                league=880_011,
                home=880_012,
                away=880_013,
                fixture=880_014,
            )
        await engine.dispose()


async def _cleanup(
    session,
    strategy_id: int,
    version_id: int,
    *,
    league: int = LEAGUE,
    home: int = HOME,
    away: int = AWAY,
    fixture: int = FIXTURE,
) -> None:
    if strategy_id:
        await session.execute(
            delete(AdvisorDecision).where(AdvisorDecision.strategy_id == strategy_id)
        )
    if version_id:
        await session.execute(
            delete(AdvisorCoupon).where(
                AdvisorCoupon.cycle_id.in_(
                    select(AdvisorCycle.id).where(
                        AdvisorCycle.strategy_version_id == version_id
                    )
                )
            )
        )
        await session.execute(
            delete(AdvisorCycle).where(AdvisorCycle.strategy_version_id == version_id)
        )
        await session.execute(
            delete(AdvisorStrategyVersion).where(
                AdvisorStrategyVersion.id == version_id
            )
        )
    if strategy_id:
        await session.execute(
            delete(AdvisorStrategy).where(AdvisorStrategy.id == strategy_id)
        )
    await session.execute(delete(Fixture).where(Fixture.id == fixture))
    await session.execute(delete(Team).where(Team.id.in_((home, away))))
    await session.execute(delete(League).where(League.id == league))


async def _seed(
    session,
    *,
    league: int = LEAGUE,
    home: int = HOME,
    away: int = AWAY,
    fixture: int = FIXTURE,
    name: str = "advisor-cycle-test",
) -> tuple[int, int]:
    session.add(League(id=league, name=name))
    session.add(Team(id=home, name="Home"))
    session.add(Team(id=away, name="Away"))
    await session.flush()
    session.add(
        Fixture(
            id=fixture,
            league_id=league,
            season=2026,
            home_team_id=home,
            away_team_id=away,
        )
    )
    strategy = AdvisorStrategy(name=name, kind="llm", status="sandbox")
    session.add(strategy)
    await session.flush()
    version = AdvisorStrategyVersion(
        strategy_id=strategy.id,
        version_no=1,
        prompt_version="p1",
        min_combined_odd=Decimal("1.600"),
        max_legs=3,
        max_minute_regulation=80,
        max_minute_extra=110,
        wait_minutes=10,
        candidate_rules=["favorite_losing"],
    )
    session.add(version)
    await session.flush()
    return version.id, strategy.id
