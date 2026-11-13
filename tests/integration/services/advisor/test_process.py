"""Advisor process: playable Over line, debounce, and an empty board."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from predictor.models.advisor import (
    AdvisorCounterfactual,
    AdvisorCoupon,
    AdvisorCycle,
    AdvisorDecision,
    AdvisorLangfuseOutbox,
    AdvisorLeg,
    AdvisorSettlementEvent,
    AdvisorStrategy,
    AdvisorStrategyEvent,
    AdvisorStrategyVersion,
)
from predictor.models.catalog import Bookmaker, League, OddsBet, OddsLiveBet
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture, Team
from predictor.models.odds import FixtureOdds, FixtureOddsLive
from predictor.models.predictions import Prediction, PredictionH2H
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.advisor import AdvisorSettings
from predictor.schemas.settings import Settings
from predictor.services.advisor.candidates import (
    MatchSnapshot,
)
from predictor.services.advisor.cycle import CycleCandidate
from predictor.services.advisor.domain import (
    PlayLimits,
)
from predictor.services.advisor.llm.protocol import LlmCompletion
from predictor.services.advisor.prompts.cycle import (
    PROMPT_VERSION,
)
from predictor.services.advisor.sql import (
    FirstOver,
    first_period_over,
    latest_over_under,
)
from predictor.services.advisor.tick import (
    BASELINE_NAME,
    LLM_NAME,
    CandidateWindow,
    _align_llm_prompt_version,
    _below_threshold_open,
    ensure_strategy,
    restore_window,
    run_tick,
)

NOW = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
_PREMATCH_BOOK = 9_701
_PREMATCH_BET = 9_702


class _Provider:
    provider_name = "google"
    model = "gemini-2.5-flash"

    def __init__(self, text: str = "{}") -> None:
        self.text = text
        self.calls = 0

    async def decide(
        self, prompt: str, *, timeout: float, response_schema: object
    ) -> LlmCompletion:
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
        max_quote_age=timedelta(seconds=300),
    )


def _match(**overrides: object) -> MatchSnapshot:
    values: dict[str, object] = {
        "fixture_id": 7,
        "status_short": "2H",
        "elapsed": 70,
        "extra": None,
        "goals_home": 0,
        "goals_away": 1,
        "extratime_home": None,
        "extratime_away": None,
        "rule": "favorite_losing",
        "favorite_odd": Decimal("1.50"),
    }
    values.update(overrides)
    return MatchSnapshot(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_below_threshold_open_reads_flat_odd_and_price_odd() -> None:
    """A5.4: restore_window re-parses legacy flat odd and p5 price.odd."""
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    name = "advisor-restore-odd-formats"
    raw = json.dumps(
        {
            "candidates": [
                {
                    "fixture_id": 9_882_001,
                    "decision": "bet",
                    "estimated_probability": "0.70",
                    "confidence": "0.60",
                    "reasons": ["favorite_pressure"],
                    "rationale": "chasing",
                }
            ],
            "coupons": [{"fixture_ids": [9_882_001]}],
        }
    )
    try:
        async with factory() as session, session.begin():
            strategy = AdvisorStrategy(name=name, kind="llm", status="sandbox")
            session.add(strategy)
            await session.flush()
            strategy_id = strategy.id
            version = AdvisorStrategyVersion(
                strategy_id=strategy_id,
                version_no=1,
                prompt_version="p5",
                min_combined_odd=Decimal("1.600"),
                max_legs=3,
                max_minute_regulation=80,
                max_minute_extra=110,
                wait_minutes=10,
                candidate_rules=["favorite_losing"],
                notes="",
            )
            session.add(version)
            await session.flush()
            version_id = version.id
            flat_body = {
                "candidates": [
                    {
                        "fixture_id": 9_882_001,
                        "period": "regulation",
                        "odd": "1.350",
                    }
                ]
            }
            session.add(
                AdvisorCycle(
                    strategy_version_id=version_id,
                    mode="sandbox",
                    provider="google",
                    model="gemini",
                    model_version="gemini",
                    prompt_version="p5",
                    prompt_text="prompt",
                    input_json=flat_body,
                    raw_output=raw,
                    validation_status="invalid_output",
                    input_tokens=1,
                    output_tokens=1,
                    thoughts_tokens=0,
                    cost_pln=Decimal("0.0100"),
                    started_at=NOW,
                )
            )
        async with factory() as session, session.begin():
            assert await _below_threshold_open(session, strategy_id) is True
            window = await restore_window(session, strategy_id)
        assert window.fired is False
        async with factory() as session, session.begin():
            session.add(
                AdvisorCycle(
                    strategy_version_id=version_id,
                    mode="sandbox",
                    provider="google",
                    model="gemini",
                    model_version="gemini",
                    prompt_version="p5",
                    prompt_text="prompt",
                    input_json={
                        "candidates": [
                            {
                                "fixture_id": 9_882_001,
                                "period": "regulation",
                                "price": {"odd": "1.350"},
                            }
                        ]
                    },
                    raw_output=raw,
                    validation_status="invalid_output",
                    input_tokens=1,
                    output_tokens=1,
                    thoughts_tokens=0,
                    cost_pln=Decimal("0.0100"),
                    started_at=NOW,
                )
            )
        async with factory() as session, session.begin():
            assert await _below_threshold_open(session, strategy_id) is True
            priced = await restore_window(session, strategy_id)
        assert priced.fired is False
    finally:
        async with factory() as session, session.begin():
            await _drop_strategies(session, (name,))
        await engine.dispose()


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
async def test_first_period_over_picks_earliest_second_half_same_score() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    league, home, away, fixture = 9_881_701, 9_881_702, 9_881_703, 9_881_704
    reg_bet, et_bet = 9_881_709, 9_881_710
    candidate = CycleCandidate(
        fixture_id=fixture,
        period="regulation",
        goals_home=0,
        goals_away=0,
        ordinal=1,
        n=0,
        odd=Decimal("1.700"),
        candidate_rule="favorite_losing",
    )
    try:
        async with factory() as session, session.begin():
            session.add(League(id=league, name="first-over"))
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
            session.add(OddsLiveBet(id=reg_bet, name="Over/Under Line"))
            session.add(OddsLiveBet(id=et_bet, name="Over/Under Extra Time"))
            await session.flush()
            session.add_all(
                [
                    FixtureOddsLive(
                        fixture_id=fixture,
                        bet_id=reg_bet,
                        value_label="Over",
                        handicap="0.5",
                        odd=Decimal("1.900"),
                        home_goals=0,
                        away_goals=0,
                        elapsed_minutes=30,
                        captured_at=NOW - timedelta(minutes=40),
                    ),
                    FixtureOddsLive(
                        fixture_id=fixture,
                        bet_id=reg_bet,
                        value_label="Over",
                        handicap="0.5",
                        odd=Decimal("1.650"),
                        home_goals=0,
                        away_goals=0,
                        elapsed_minutes=46,
                        captured_at=NOW - timedelta(minutes=20),
                    ),
                    FixtureOddsLive(
                        fixture_id=fixture,
                        bet_id=reg_bet,
                        value_label="Over",
                        handicap="0.5",
                        odd=Decimal("1.700"),
                        home_goals=0,
                        away_goals=0,
                        elapsed_minutes=60,
                        captured_at=NOW - timedelta(minutes=5),
                    ),
                    FixtureOddsLive(
                        fixture_id=fixture,
                        bet_id=reg_bet,
                        value_label="Over",
                        handicap="0.5",
                        odd=Decimal("1.100"),
                        home_goals=0,
                        away_goals=0,
                        elapsed_minutes=50,
                        suspended=True,
                        captured_at=NOW - timedelta(minutes=25),
                    ),
                ]
            )
        async with factory() as session, session.begin():
            found = await first_period_over(session, (candidate,))
        assert found[fixture] == FirstOver(odd=Decimal("1.650"), minute=46)
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                delete(FixtureOddsLive).where(FixtureOddsLive.fixture_id == fixture)
            )
            await session.execute(
                delete(OddsLiveBet).where(OddsLiveBet.id.in_((reg_bet, et_bet)))
            )
            await session.execute(delete(Fixture).where(Fixture.id == fixture))
            await session.execute(delete(Team).where(Team.id.in_((home, away))))
            await session.execute(delete(League).where(League.id == league))
        await engine.dispose()


@pytest.mark.asyncio
async def test_first_period_over_after_goal_and_extra_time_market() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    league, home, away = 9_881_721, 9_881_722, 9_881_723
    fixture_reg, fixture_et = 9_881_724, 9_881_725
    reg_bet, et_bet = 9_881_729, 9_881_730
    try:
        async with factory() as session, session.begin():
            session.add(League(id=league, name="first-over-et"))
            session.add(Team(id=home, name="Home"))
            session.add(Team(id=away, name="Away"))
            await session.flush()
            session.add_all(
                [
                    Fixture(
                        id=fixture_reg,
                        league_id=league,
                        season=2026,
                        home_team_id=home,
                        away_team_id=away,
                    ),
                    Fixture(
                        id=fixture_et,
                        league_id=league,
                        season=2026,
                        home_team_id=home,
                        away_team_id=away,
                    ),
                    OddsLiveBet(id=reg_bet, name="Over/Under Line"),
                    OddsLiveBet(id=et_bet, name="Over/Under Extra Time"),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    FixtureOddsLive(
                        fixture_id=fixture_reg,
                        bet_id=reg_bet,
                        value_label="Over",
                        handicap="0.5",
                        odd=Decimal("1.800"),
                        home_goals=0,
                        away_goals=0,
                        elapsed_minutes=50,
                        captured_at=NOW - timedelta(minutes=30),
                    ),
                    FixtureOddsLive(
                        fixture_id=fixture_reg,
                        bet_id=reg_bet,
                        value_label="Over",
                        handicap="1.5",
                        odd=Decimal("1.550"),
                        home_goals=1,
                        away_goals=0,
                        elapsed_minutes=62,
                        captured_at=NOW - timedelta(minutes=10),
                    ),
                    FixtureOddsLive(
                        fixture_id=fixture_reg,
                        bet_id=reg_bet,
                        value_label="Over",
                        handicap="1.5",
                        odd=Decimal("1.600"),
                        home_goals=1,
                        away_goals=0,
                        elapsed_minutes=70,
                        captured_at=NOW - timedelta(minutes=2),
                    ),
                    FixtureOddsLive(
                        fixture_id=fixture_et,
                        bet_id=reg_bet,
                        value_label="Over",
                        handicap="0.5",
                        odd=Decimal("1.200"),
                        home_goals=1,
                        away_goals=1,
                        elapsed_minutes=100,
                        captured_at=NOW - timedelta(minutes=8),
                    ),
                    FixtureOddsLive(
                        fixture_id=fixture_et,
                        bet_id=et_bet,
                        value_label="Over",
                        handicap="0.5",
                        odd=Decimal("1.450"),
                        home_goals=1,
                        away_goals=1,
                        elapsed_minutes=95,
                        captured_at=NOW - timedelta(minutes=6),
                    ),
                    FixtureOddsLive(
                        fixture_id=fixture_et,
                        bet_id=et_bet,
                        value_label="Over",
                        handicap="0.5",
                        odd=Decimal("1.500"),
                        home_goals=1,
                        away_goals=1,
                        elapsed_minutes=105,
                        captured_at=NOW - timedelta(minutes=1),
                    ),
                ]
            )
        async with factory() as session, session.begin():
            found = await first_period_over(
                session,
                (
                    CycleCandidate(
                        fixture_id=fixture_reg,
                        period="regulation",
                        goals_home=1,
                        goals_away=0,
                        ordinal=2,
                        n=1,
                        odd=Decimal("1.600"),
                        candidate_rule="favorite_losing",
                    ),
                    CycleCandidate(
                        fixture_id=fixture_et,
                        period="extra_time",
                        goals_home=1,
                        goals_away=1,
                        ordinal=1,
                        n=0,
                        odd=Decimal("1.500"),
                        candidate_rule="tie_deficit",
                    ),
                ),
            )
        assert found[fixture_reg] == FirstOver(odd=Decimal("1.550"), minute=62)
        assert found[fixture_et] == FirstOver(odd=Decimal("1.450"), minute=95)
        async with factory() as session, session.begin():
            empty = await first_period_over(
                session,
                (
                    CycleCandidate(
                        fixture_id=fixture_reg,
                        period="regulation",
                        goals_home=2,
                        goals_away=2,
                        ordinal=5,
                        n=4,
                        odd=Decimal("1.900"),
                        candidate_rule="favorite_losing",
                    ),
                ),
            )
        assert empty == {}
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                delete(FixtureOddsLive).where(
                    FixtureOddsLive.fixture_id.in_((fixture_reg, fixture_et))
                )
            )
            await session.execute(
                delete(OddsLiveBet).where(OddsLiveBet.id.in_((reg_bet, et_bet)))
            )
            await session.execute(
                delete(Fixture).where(Fixture.id.in_((fixture_reg, fixture_et)))
            )
            await session.execute(delete(Team).where(Team.id.in_((home, away))))
            await session.execute(delete(League).where(League.id == league))
        await engine.dispose()


@pytest.mark.asyncio
async def test_llm_strategy_gains_prompt_version_once() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    name = "sandbox-llm-p5-bump"
    try:
        async with factory() as session, session.begin():
            strategy = AdvisorStrategy(name=name, kind="llm", status="sandbox")
            session.add(strategy)
            await session.flush()
            strategy_id = strategy.id
            first = AdvisorStrategyVersion(
                strategy_id=strategy_id,
                version_no=1,
                prompt_version="p4",
                min_combined_odd=Decimal("1.650"),
                max_legs=2,
                max_minute_regulation=75,
                max_minute_extra=105,
                wait_minutes=8,
                candidate_rules=["favorite_losing"],
                notes="old",
            )
            session.add(first)
            await session.flush()
            bumped = await _align_llm_prompt_version(session, first)
            again = await _align_llm_prompt_version(session, bumped)
            versions = (
                await session.scalars(
                    select(AdvisorStrategyVersion)
                    .where(AdvisorStrategyVersion.strategy_id == strategy_id)
                    .order_by(AdvisorStrategyVersion.version_no)
                )
            ).all()
        assert len(versions) == 2
        assert versions[0].prompt_version == "p4"
        assert versions[0].notes == "old"
        assert versions[1].id == bumped.id == again.id
        assert versions[1].prompt_version == PROMPT_VERSION
        assert versions[1].version_no == 2
        assert versions[1].min_combined_odd == Decimal("1.650")
        assert versions[1].max_legs == 2
        assert versions[1].max_minute_regulation == 75
        assert versions[1].max_minute_extra == 105
        assert versions[1].wait_minutes == 8
        assert versions[1].candidate_rules == ["favorite_losing"]
        assert versions[1].notes == f"prompt {PROMPT_VERSION}"
    finally:
        async with factory() as session, session.begin():
            await _drop_strategies(session, (name,))
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
async def test_paused_named_strategy_is_reused() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    name = "advisor-paused-baseline"
    try:
        async with factory() as session, session.begin():
            paused = AdvisorStrategy(name=name, kind="baseline", status="paused")
            session.add(paused)
            await session.flush()
            paused_id = paused.id
            session.add(
                AdvisorStrategyVersion(
                    strategy_id=paused_id,
                    version_no=1,
                    prompt_version="p4",
                    min_combined_odd=Decimal("1.600"),
                    max_legs=3,
                    max_minute_regulation=80,
                    max_minute_extra=110,
                    wait_minutes=10,
                    candidate_rules=["favorite_losing"],
                    notes="",
                )
            )
        async with factory() as session, session.begin():
            active = await session.scalar(
                select(AdvisorStrategy.id).where(
                    AdvisorStrategy.kind == "baseline",
                    AdvisorStrategy.status.in_(("sandbox", "promoted")),
                )
            )
            book = await ensure_strategy(session, name, "baseline")
            named = (
                await session.scalars(
                    select(AdvisorStrategy).where(AdvisorStrategy.name == name)
                )
            ).all()
        assert len(named) == 1
        assert named[0].status == "paused"
        if active is None:
            assert book.strategy_id == paused_id
            assert book.status == "paused"
    finally:
        async with factory() as session, session.begin():
            await _drop_strategies(session, (name,))
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


@pytest.mark.asyncio
async def test_lone_weak_candidate_does_not_call_the_model() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    provider = _Provider(
        json.dumps(
            {
                "candidates": [
                    {
                        "fixture_id": 880_754,
                        "decision": "wait",
                        "estimated_probability": "0.60",
                        "confidence": "0.40",
                        "reasons": ["favorite_pressure"],
                        "rationale": "waiting for a second leg",
                    },
                    {
                        "fixture_id": 880_784,
                        "decision": "wait",
                        "estimated_probability": "0.60",
                        "confidence": "0.40",
                        "reasons": ["favorite_pressure"],
                        "rationale": "waiting for a second leg",
                    },
                ],
                "coupons": [],
            }
        )
    )
    window = CandidateWindow()
    try:
        async with factory() as session, session.begin():
            await _seed_live(session, 880_751, odd="1.700")
        assert (
            await run_tick(engine, factory, provider, _settings(), window, NOW) is False
        )
        assert (
            await run_tick(
                engine,
                factory,
                provider,
                _settings(),
                window,
                NOW + timedelta(seconds=20),
            )
            is False
        )
        assert provider.calls == 0
        # Partner uses 880_781 — keep clear of 880_761 refresh tests.
        async with factory() as session, session.begin():
            await _seed_live(session, 880_781, odd="1.850")
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
        called = await run_tick(
            engine,
            factory,
            provider,
            _settings(),
            window,
            NOW + timedelta(seconds=60),
        )
        assert called is True
        assert provider.calls == 1
    finally:
        async with factory() as session, session.begin():
            await _drop_live(session, 880_781)
            await _drop_live(session, 880_751)
        await engine.dispose()


def _bet_reply(fixture_id: int) -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "fixture_id": fixture_id,
                    "decision": "bet",
                    "estimated_probability": "0.70",
                    "confidence": "0.60",
                    "reasons": ["favorite_pressure"],
                    "rationale": "the favourite is chasing",
                }
            ],
            "coupons": [{"fixture_ids": [fixture_id]}],
        }
    )


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
async def test_score_mismatch_outside_llm_gate_does_not_enqueue() -> None:
    from predictor.models.advisor import AdvisorOddsRefresh

    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    # Playable LLM leg opens the window; a stale sibling is out of the gate.
    provider = _Provider(_wait_reply(880_764))
    window = CandidateWindow()
    try:
        async with factory() as session, session.begin():
            await _seed_live(session, 880_761)
            await _seed_live(session, 880_771)
            await session.execute(
                delete(FixtureOddsLive).where(FixtureOddsLive.fixture_id == 880_774)
            )
            session.add(
                FixtureOddsLive(
                    fixture_id=880_774,
                    bet_id=880_779,
                    value_label="Over",
                    handicap="1.5",
                    odd=Decimal("1.850"),
                    home_goals=0,
                    away_goals=0,
                    captured_at=NOW - timedelta(seconds=10),
                )
            )
        assert (
            await run_tick(engine, factory, provider, _settings(), window, NOW) is False
        )
        called = await run_tick(
            engine,
            factory,
            provider,
            _settings(),
            window,
            NOW + timedelta(seconds=20),
        )
        assert called is True
        assert provider.calls == 1
        async with factory() as session:
            pending = list(await session.scalars(select(AdvisorOddsRefresh.fixture_id)))
        assert pending == []
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(AdvisorOddsRefresh))
            await _drop_live(session, 880_771)
            await _drop_live(session, 880_761)
        await engine.dispose()


@pytest.mark.asyncio
async def test_refresh_wait_timeout_enqueues_and_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from predictor.models.advisor import AdvisorOddsRefresh
    from predictor.services.advisor import tick as tick_mod
    from predictor.services.advisor.sql import OverUnderRow
    from predictor.services.advisor.tick import _refresh_before_llm

    clock = {"t": 0.0}
    enqueued: list[tuple[int, ...]] = []
    real_enqueue = tick_mod.enqueue_odds_refresh

    async def spy(session, fixture_ids, *, now):
        ids = await real_enqueue(session, fixture_ids, now=now)
        enqueued.append(ids)
        return ids

    monkeypatch.setattr(tick_mod, "enqueue_odds_refresh", spy)

    async def jump(seconds: float) -> None:
        clock["t"] += seconds + 20.0

    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            await _seed_live(session, 880_761)
            await session.execute(
                delete(FixtureOddsLive).where(FixtureOddsLive.fixture_id == 880_764)
            )
            session.add(
                FixtureOddsLive(
                    fixture_id=880_764,
                    bet_id=880_769,
                    value_label="Over",
                    handicap="1.5",
                    odd=Decimal("1.850"),
                    home_goals=0,
                    away_goals=0,
                    captured_at=NOW,
                )
            )
            book = await ensure_strategy(session, LLM_NAME, "llm")
        selected = (_match(fixture_id=880_764),)
        stale = OverUnderRow(
            fixture_id=880_764,
            label="Over",
            handicap="1.5",
            odd=Decimal("1.850"),
            tradable=True,
            captured_at=NOW,
            home_goals=0,
            away_goals=0,
        )
        gated = CycleCandidate(
            fixture_id=880_764,
            period="regulation",
            goals_home=0,
            goals_away=1,
            ordinal=2,
            candidate_rule="favorite_losing",
            odd=Decimal("1.850"),
            n=1,
        )
        result = await _refresh_before_llm(
            engine,
            factory,
            book,
            selected,
            {880_764: (stale,)},
            NOW,
            candidates=(gated,),
            monotonic=lambda: clock["t"],
            sleep=jump,
        )
        assert result is None
        assert enqueued == [(880_764,)]
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(AdvisorOddsRefresh))
            await _drop_live(session, 880_761)
        await engine.dispose()


@pytest.mark.asyncio
async def test_refresh_unblocks_after_worker_write() -> None:
    from predictor.models.advisor import AdvisorOddsRefresh
    from predictor.services.advisor.sql import OverUnderRow
    from predictor.services.advisor.tick import _refresh_before_llm

    clock = {"t": 0.0}

    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            await _seed_live(session, 880_761)
            await session.execute(
                delete(FixtureOddsLive).where(FixtureOddsLive.fixture_id == 880_764)
            )
            session.add(
                FixtureOddsLive(
                    fixture_id=880_764,
                    bet_id=880_769,
                    value_label="Over",
                    handicap="1.5",
                    odd=Decimal("1.850"),
                    home_goals=0,
                    away_goals=0,
                    captured_at=NOW - timedelta(seconds=30),
                )
            )
            book = await ensure_strategy(session, LLM_NAME, "llm")

        async def write_on_poll(seconds: float) -> None:
            clock["t"] += seconds
            async with factory() as session, session.begin():
                session.add(
                    FixtureOddsLive(
                        fixture_id=880_764,
                        bet_id=880_769,
                        value_label="Over",
                        handicap="1.5",
                        odd=Decimal("1.900"),
                        home_goals=0,
                        away_goals=1,
                        captured_at=NOW,
                    )
                )
                await session.execute(
                    delete(AdvisorOddsRefresh).where(
                        AdvisorOddsRefresh.fixture_id == 880_764
                    )
                )

        selected = (_match(fixture_id=880_764),)
        stale = OverUnderRow(
            fixture_id=880_764,
            label="Over",
            handicap="1.5",
            odd=Decimal("1.850"),
            tradable=True,
            captured_at=NOW - timedelta(seconds=30),
            home_goals=0,
            away_goals=0,
        )
        gated = CycleCandidate(
            fixture_id=880_764,
            period="regulation",
            goals_home=0,
            goals_away=1,
            ordinal=2,
            candidate_rule="favorite_losing",
            odd=Decimal("1.850"),
            n=1,
        )
        result = await _refresh_before_llm(
            engine,
            factory,
            book,
            selected,
            {880_764: (stale,)},
            NOW,
            candidates=(gated,),
            monotonic=lambda: clock["t"],
            sleep=write_on_poll,
        )
        assert result is not None
        _selected, _quotes, candidates, _opened = result
        assert len(candidates) == 1
        assert candidates[0].fixture_id == 880_764
        assert candidates[0].odd == Decimal("1.900")
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(AdvisorOddsRefresh))
            await _drop_live(session, 880_761)
        await engine.dispose()


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


def _bet_one_reject_other(bet_id: int, reject_id: int) -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "fixture_id": bet_id,
                    "decision": "bet",
                    "estimated_probability": "0.70",
                    "confidence": "0.60",
                    "reasons": ["favorite_pressure"],
                    "rationale": "the favourite is chasing",
                },
                {
                    "fixture_id": reject_id,
                    "decision": "reject",
                    "estimated_probability": "0.40",
                    "confidence": "0.50",
                    "reasons": ["weak_favorite"],
                    "rationale": "partner too weak",
                },
            ],
            "coupons": [{"fixture_ids": [bet_id]}],
        }
    )


@pytest.mark.asyncio
async def test_below_threshold_sends_the_same_set_again() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    provider = _Provider(_bet_one_reject_other(880_734, 880_744))
    window = CandidateWindow()
    try:
        async with factory() as session, session.begin():
            # Two legs so G4 allows the call; model singles one below 1.60.
            await _seed_live(session, 880_731, odd="1.450")
            await _seed_live(session, 880_741, odd="1.450")
        assert (
            await run_tick(engine, factory, provider, _settings(), window, NOW) is False
        )
        first = await run_tick(
            engine,
            factory,
            provider,
            _settings(),
            window,
            NOW + timedelta(seconds=20),
        )
        assert first is True
        assert provider.calls == 1
        assert window.fired is False
        assert (
            await run_tick(
                engine,
                factory,
                provider,
                _settings(),
                window,
                NOW + timedelta(seconds=30),
            )
            is False
        )
        assert provider.calls == 1
        second = await run_tick(
            engine,
            factory,
            provider,
            _settings(),
            window,
            NOW + timedelta(seconds=40),
        )
        assert second is True
        assert provider.calls == 2
    finally:
        async with factory() as session, session.begin():
            await _drop_live(session, 880_741)
            await _drop_live(session, 880_731)
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


async def _seed_live(session, base: int, *, odd: str = "1.850") -> None:
    league, home, away, fixture = base, base + 1, base + 2, base + 3
    await _drop_live(session, base)
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
    if await session.get(Bookmaker, _PREMATCH_BOOK) is None:
        session.add(Bookmaker(id=_PREMATCH_BOOK, name="advisor-test-book"))
    if await session.get(OddsBet, _PREMATCH_BET) is None:
        session.add(OddsBet(id=_PREMATCH_BET, name="Match Winner"))
    await session.flush()
    for label, price in (("Home", "1.50"), ("Draw", "3.80"), ("Away", "5.50")):
        session.add(
            FixtureOdds(
                fixture_id=fixture,
                bookmaker_id=_PREMATCH_BOOK,
                bet_id=_PREMATCH_BET,
                value_label=label,
                odd=Decimal(price),
            )
        )
    live_task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.fixture_id.is_(None))
        .where(EtlTask.day_utc.is_(None))
        .where(EtlTask.params.contains({"live": "all"}))
        .order_by(EtlTask.id)
        .limit(1)
    )
    params = {"live": "all", "fixture_ids": [fixture]}
    if live_task is None:
        session.add(EtlTask(endpoint="/fixtures", params=params, status="pending"))
    else:
        # Keep every seeded live fixture visible on the board.
        existing = list(live_task.params.get("fixture_ids") or [])
        if fixture not in existing:
            existing.append(fixture)
        live_task.params = {"live": "all", "fixture_ids": existing}
    session.add(OddsLiveBet(id=base + 8, name="Over/Under Line"))
    await session.flush()
    session.add(
        FixtureOddsLive(
            fixture_id=fixture,
            bet_id=base + 8,
            value_label="Over",
            handicap="1.5",
            odd=Decimal(odd),
            home_goals=0,
            away_goals=1,
            captured_at=NOW - timedelta(seconds=10),
        )
    )


async def _drop_live(session, base: int) -> None:
    league, home, away, fixture = base, base + 1, base + 2, base + 3
    await _drop_strategies(session)
    await session.execute(
        delete(FixtureOddsLive).where(FixtureOddsLive.fixture_id == fixture)
    )
    await session.execute(delete(FixtureOdds).where(FixtureOdds.fixture_id == fixture))
    await session.execute(delete(OddsLiveBet).where(OddsLiveBet.id == base + 8))
    await session.execute(delete(Prediction).where(Prediction.fixture_id == fixture))
    await session.execute(
        delete(PredictionH2H).where(
            (PredictionH2H.fixture_id == fixture)
            | (PredictionH2H.h2h_fixture_id == fixture)
        )
    )
    live_task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == "/fixtures")
        .where(EtlTask.cursor_kind.is_(None))
        .where(EtlTask.fixture_id.is_(None))
        .where(EtlTask.day_utc.is_(None))
        .where(EtlTask.params.contains({"live": "all"}))
        .order_by(EtlTask.id)
        .limit(1)
    )
    if live_task is not None:
        kept = [
            item
            for item in (live_task.params.get("fixture_ids") or [])
            if item != fixture
        ]
        live_task.params = {"live": "all", "fixture_ids": kept}
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
    coupons = select(AdvisorCoupon.id).where(
        AdvisorCoupon.strategy_version_id.in_(versions)
    )
    legs = select(AdvisorLeg.id).where(AdvisorLeg.strategy_id.in_(ids))
    decisions = select(AdvisorDecision.id).where(AdvisorDecision.strategy_id.in_(ids))
    await session.execute(
        delete(AdvisorSettlementEvent).where(
            AdvisorSettlementEvent.coupon_id.in_(coupons)
            | AdvisorSettlementEvent.leg_id.in_(legs)
        )
    )
    await session.execute(
        delete(AdvisorCounterfactual).where(
            AdvisorCounterfactual.decision_id.in_(decisions)
        )
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
    await session.execute(
        delete(AdvisorStrategyEvent).where(AdvisorStrategyEvent.strategy_id.in_(ids))
    )
    await session.execute(delete(AdvisorStrategy).where(AdvisorStrategy.id.in_(ids)))
