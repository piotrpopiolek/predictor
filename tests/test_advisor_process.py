"""Advisor process: playable Over line, debounce, and an empty board."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorCycle,
    AdvisorDecision,
    AdvisorLangfuseOutbox,
    AdvisorLeg,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.models.catalog import League, OddsLiveBet
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture, Team
from predictor.models.odds import FixtureOddsLive
from predictor.models.predictions import Prediction
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.advisor import AdvisorSettings
from predictor.schemas.settings import Settings
from predictor.services.advisor.candidates import (
    MatchSnapshot,
    playable_candidate,
)
from predictor.services.advisor.domain import (
    MIN_ANALYSIS_ODD,
    PlayLimits,
    over_line_matches,
)
from predictor.services.advisor.llm.protocol import LlmCompletion
from predictor.services.advisor.prompts.cycle import render_cycle_prompt
from predictor.services.advisor.sql import latest_over_under
from predictor.services.advisor.tick import (
    BASELINE_NAME,
    LLM_NAME,
    CandidateWindow,
    ensure_strategy,
    run_tick,
)

NOW = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)


class _Provider:
    provider_name = "google"
    model = "gemini-2.5-flash"

    def __init__(self, text: str = "{}") -> None:
        self.text = text
        self.calls = 0

    async def decide(self, prompt: str, *, timeout: float) -> LlmCompletion:
        self.calls += 1
        return LlmCompletion(
            text=self.text,
            provider=self.provider_name,
            model=self.model,
            model_version="gemini-2.5-flash-001",
            input_tokens=1,
            output_tokens=1,
            thoughts_tokens=0,
        )


def _settings() -> AdvisorSettings:
    return AdvisorSettings(
        llm_api_key="secret-key-value",
        llm_price_input_pln_per_mtok=Decimal("0.50"),
        llm_price_output_pln_per_mtok=Decimal("1.25"),
        advisor_debounce_seconds=20,
    )


def _limits() -> PlayLimits:
    return PlayLimits(
        max_minute_regulation=80,
        max_minute_extra=110,
        max_quote_age=timedelta(seconds=90),
    )


def _match() -> MatchSnapshot:
    return MatchSnapshot(
        fixture_id=7,
        status_short="2H",
        elapsed=70,
        extra=None,
        goals_home=0,
        goals_away=1,
        extratime_home=None,
        extratime_away=None,
        rule="favorite_losing",
    )


def test_over_line_matches_the_driver_total_and_not_another() -> None:
    assert over_line_matches("Over", "1.5", 1)
    assert over_line_matches("Over", "1.50", 1)
    assert over_line_matches("Over 1.5", "", 1)
    assert not over_line_matches("Over", "2.5", 1)
    assert not over_line_matches("Under", "1.5", 1)


def test_window_waits_for_a_stable_set_and_fires_once() -> None:
    window = CandidateWindow()
    signature = ((7, "regulation", 0, 1, 2),)
    assert window.ready(signature, NOW, timedelta(seconds=20)) is False
    assert (
        window.ready(signature, NOW + timedelta(seconds=19), timedelta(seconds=20))
        is False
    )
    assert (
        window.ready(signature, NOW + timedelta(seconds=20), timedelta(seconds=20))
        is True
    )
    assert (
        window.ready(signature, NOW + timedelta(seconds=21), timedelta(seconds=20))
        is True
    )
    window.mark_fired()
    assert (
        window.ready(signature, NOW + timedelta(seconds=40), timedelta(seconds=20))
        is False
    )
    assert window.next_sleep(NOW, timedelta(seconds=20), 15) == 15
    fresh = CandidateWindow()
    fresh.ready(signature, NOW, timedelta(seconds=20))
    assert fresh.next_sleep(NOW + timedelta(seconds=15), timedelta(seconds=20), 15) == 5
    assert window.ready((), NOW + timedelta(seconds=41), timedelta(seconds=20)) is False


def test_playable_candidate_uses_over_n_plus_half() -> None:
    from predictor.services.advisor.sql import OverUnderRow

    row = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=Decimal("1.70"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
    )
    candidate = playable_candidate(
        _match(),
        (row,),
        limits=_limits(),
        now=NOW,
        open_fixtures=frozenset(),
        rejected=frozenset(),
    )
    assert candidate is not None
    assert candidate.n == 1
    assert candidate.odd == Decimal("1.70")
    assert candidate.ordinal == 2
    assert (
        playable_candidate(
            _match(),
            (row,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset({(7, "regulation", 0, 1, 2)}),
        )
        is None
    )
    stale = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=Decimal("1.70"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=120),
    )
    assert (
        playable_candidate(
            _match(),
            (stale,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
        )
        is None
    )


def test_playable_candidate_starts_analysis_from_min_analysis_odd() -> None:
    from predictor.services.advisor.sql import OverUnderRow

    priced = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=MIN_ANALYSIS_ODD,
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
    )
    assert (
        playable_candidate(
            _match(),
            (priced,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
        )
        is not None
    )
    below = OverUnderRow(
        fixture_id=7,
        label="Over",
        handicap="1.5",
        odd=MIN_ANALYSIS_ODD - Decimal("0.001"),
        tradable=True,
        captured_at=NOW - timedelta(seconds=10),
    )
    assert (
        playable_candidate(
            _match(),
            (below,),
            limits=_limits(),
            now=NOW,
            open_fixtures=frozenset(),
            rejected=frozenset(),
        )
        is None
    )


def test_cycle_prompt_asks_for_multi_leg_coupons_to_min_combined() -> None:
    from predictor.services.advisor.cycle import CycleCandidate

    candidates = (
        CycleCandidate(
            fixture_id=7,
            period="regulation",
            goals_home=0,
            goals_away=1,
            ordinal=2,
            n=1,
            odd=Decimal("1.300"),
            candidate_rule="favorite_losing",
        ),
        CycleCandidate(
            fixture_id=8,
            period="regulation",
            goals_home=1,
            goals_away=1,
            ordinal=3,
            n=2,
            odd=Decimal("1.400"),
            candidate_rule="tie_deficit",
        ),
    )
    prompt, body = render_cycle_prompt(
        candidates,
        min_combined_odd=Decimal("1.600"),
        max_legs=3,
        match_details={
            7: {
                "stats": [{"label": "Strzały", "home": "8", "away": "3"}],
                "events": [],
            }
        },
    )
    assert body["min_analysis_odd"] == "1.300"
    assert body["min_combined_odd"] == "1.600"
    assert body["max_legs"] == 3
    assert body["candidates"][0]["detail"]["stats"][0]["label"] == "Strzały"
    assert body["candidates"][1]["detail"] is None
    assert "Prefer coupons of 2–3 legs" in prompt
    assert ">= 1.600" in prompt
    assert "odd >= 1.300" in prompt
    assert "candidate.detail is the full /live/match/" in prompt


@pytest.mark.asyncio
async def test_latest_over_under_keeps_the_newest_snapshot() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                session.add(League(id=880_601, name="advisor-quote"))
                session.add(Team(id=880_602, name="Home"))
                session.add(Team(id=880_603, name="Away"))
                await session.flush()
                session.add(
                    Fixture(
                        id=880_604,
                        league_id=880_601,
                        season=2026,
                        home_team_id=880_602,
                        away_team_id=880_603,
                    )
                )
                session.add(OddsLiveBet(id=880_609, name="Over/Under Line"))
                await session.flush()
                session.add(
                    FixtureOddsLive(
                        fixture_id=880_604,
                        bet_id=880_609,
                        value_label="Over",
                        handicap="1.5",
                        odd=Decimal("1.200"),
                        captured_at=NOW - timedelta(minutes=5),
                    )
                )
                session.add(
                    FixtureOddsLive(
                        fixture_id=880_604,
                        bet_id=880_609,
                        value_label="Over",
                        handicap="1.5",
                        odd=Decimal("1.800"),
                        suspended=False,
                        captured_at=NOW,
                    )
                )
            async with session.begin():
                rows = await latest_over_under(session, [880_604])
            assert [item.odd for item in rows[880_604]] == [Decimal("1.800")]
            assert rows[880_604][0].tradable is True
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                delete(FixtureOddsLive).where(FixtureOddsLive.fixture_id == 880_604)
            )
            await session.execute(delete(OddsLiveBet).where(OddsLiveBet.id == 880_609))
            await session.execute(delete(Fixture).where(Fixture.id == 880_604))
            await session.execute(delete(Team).where(Team.id.in_((880_602, 880_603))))
            await session.execute(delete(League).where(League.id == 880_601))
        await engine.dispose()


@pytest.mark.asyncio
async def test_retired_strategy_does_not_block_the_named_sandbox() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                session.add(
                    AdvisorStrategy(name="retired-llm", kind="llm", status="retired")
                )
                await session.flush()
                book = await ensure_strategy(session, LLM_NAME, "llm")
            async with session.begin():
                chosen = await session.get(AdvisorStrategy, book.strategy_id)
                again = await ensure_strategy(session, LLM_NAME, "llm")
            assert chosen is not None
            assert chosen.name == LLM_NAME
            assert chosen.status == "sandbox"
            assert again.strategy_id == book.strategy_id
    finally:
        async with factory() as session, session.begin():
            await _drop_strategies(session, ("retired-llm", LLM_NAME))
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_board_does_not_call_the_model() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    provider = _Provider()
    try:
        called = await run_tick(
            engine,
            factory,
            provider,
            _settings(),
            CandidateWindow(),
            NOW,
        )
        assert called is False
        assert provider.calls == 0
    finally:
        async with factory() as session, session.begin():
            await _drop_strategies(session)
        await engine.dispose()


def _wait_reply(fixture_id: int) -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "fixture_id": fixture_id,
                    "decision": "wait",
                    "estimated_probability": "0.60",
                    "confidence": "0.40",
                    "reasons": ["favorite_pressure"],
                    "rationale": "waiting for a second leg",
                }
            ],
            "coupons": [],
        }
    )


def _tight_budget() -> AdvisorSettings:
    return AdvisorSettings(
        llm_api_key="secret-key-value",
        llm_price_input_pln_per_mtok=Decimal("0.50"),
        llm_price_output_pln_per_mtok=Decimal("1.25"),
        advisor_daily_budget_pln=Decimal("0.001"),
        advisor_debounce_seconds=20,
    )


@pytest.mark.asyncio
async def test_a_stable_set_calls_the_model_once() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    provider = _Provider(_wait_reply(880_704))
    window = CandidateWindow()
    try:
        async with factory() as session, session.begin():
            await _seed_live(session, 880_701)
        assert (
            await run_tick(engine, factory, provider, _settings(), window, NOW) is False
        )
        assert provider.calls == 0
        assert (
            await run_tick(
                engine,
                factory,
                provider,
                _settings(),
                window,
                NOW + timedelta(seconds=20),
            )
            is True
        )
        assert provider.calls == 1
        assert (
            await run_tick(
                engine,
                factory,
                provider,
                _settings(),
                window,
                NOW + timedelta(seconds=40),
            )
            is False
        )
        assert provider.calls == 1
    finally:
        async with factory() as session, session.begin():
            await _drop_live(session, 880_701)
        await engine.dispose()


@pytest.mark.asyncio
async def test_budget_block_leaves_the_set_for_the_next_tick() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    provider = _Provider(_wait_reply(880_714))
    window = CandidateWindow()
    try:
        async with factory() as session, session.begin():
            await _seed_live(session, 880_711)
        assert (
            await run_tick(engine, factory, provider, _tight_budget(), window, NOW)
            is False
        )
        blocked = await run_tick(
            engine,
            factory,
            provider,
            _tight_budget(),
            window,
            NOW + timedelta(seconds=20),
        )
        assert blocked is False
        assert provider.calls == 0
        assert window.fired is False
        called = await run_tick(
            engine,
            factory,
            provider,
            _settings(),
            window,
            NOW + timedelta(seconds=21),
        )
        assert called is True
        assert provider.calls == 1
    finally:
        async with factory() as session, session.begin():
            await _drop_live(session, 880_711)
        await engine.dispose()


async def _seed_live(session, base: int) -> None:
    league, home, away, fixture = base, base + 1, base + 2, base + 3
    session.add(League(id=league, name=f"advisor-live-{base}"))
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
            status_short="2H",
            elapsed_minutes=70,
            goals_home=0,
            goals_away=1,
        )
    )
    session.add(Prediction(fixture_id=fixture, winner_team_id=home))
    session.add(
        EtlTask(
            endpoint="/fixtures",
            params={"live": "all", "fixture_ids": [fixture]},
            status="pending",
        )
    )
    session.add(OddsLiveBet(id=base + 8, name="Over/Under Line"))
    await session.flush()
    session.add(
        FixtureOddsLive(
            fixture_id=fixture,
            bet_id=base + 8,
            value_label="Over",
            handicap="1.5",
            odd=Decimal("1.700"),
            captured_at=NOW - timedelta(seconds=10),
        )
    )


async def _drop_live(session, base: int) -> None:
    league, home, away, fixture = base, base + 1, base + 2, base + 3
    await _drop_strategies(session)
    await session.execute(
        delete(FixtureOddsLive).where(FixtureOddsLive.fixture_id == fixture)
    )
    await session.execute(delete(OddsLiveBet).where(OddsLiveBet.id == base + 8))
    await session.execute(delete(Prediction).where(Prediction.fixture_id == fixture))
    await session.execute(
        delete(EtlTask).where(EtlTask.params.contains({"fixture_ids": [fixture]}))
    )
    await session.execute(delete(Fixture).where(Fixture.id == fixture))
    await session.execute(delete(Team).where(Team.id.in_((home, away))))
    await session.execute(delete(League).where(League.id == league))


async def _drop_strategies(
    session, names: tuple[str, ...] = (BASELINE_NAME, LLM_NAME)
) -> None:
    rows = (
        await session.scalars(
            select(AdvisorStrategy).where(AdvisorStrategy.name.in_(names))
        )
    ).all()
    ids = [row.id for row in rows]
    if not ids:
        return
    versions = select(AdvisorStrategyVersion.id).where(
        AdvisorStrategyVersion.strategy_id.in_(ids)
    )
    cycles = select(AdvisorCycle.id).where(
        AdvisorCycle.strategy_version_id.in_(versions)
    )
    await session.execute(delete(AdvisorLeg).where(AdvisorLeg.strategy_id.in_(ids)))
    await session.execute(
        delete(AdvisorDecision).where(AdvisorDecision.strategy_id.in_(ids))
    )
    await session.execute(
        delete(AdvisorLangfuseOutbox).where(AdvisorLangfuseOutbox.cycle_id.in_(cycles))
    )
    await session.execute(
        delete(AdvisorCoupon).where(AdvisorCoupon.strategy_version_id.in_(versions))
    )
    await session.execute(
        delete(AdvisorCycle).where(AdvisorCycle.strategy_version_id.in_(versions))
    )
    await session.execute(
        delete(AdvisorStrategyVersion).where(
            AdvisorStrategyVersion.strategy_id.in_(ids)
        )
    )
    await session.execute(delete(AdvisorStrategy).where(AdvisorStrategy.id.in_(ids)))
