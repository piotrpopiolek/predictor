"""Advisor process: playable Over line, debounce, and an empty board."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from predictor.models.advisor import (
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.models.catalog import League, OddsLiveBet
from predictor.models.fixtures import Fixture, Team
from predictor.models.odds import FixtureOddsLive
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.advisor import AdvisorSettings
from predictor.schemas.settings import Settings
from predictor.services.advisor.candidates import (
    MatchSnapshot,
    playable_candidate,
)
from predictor.services.advisor.domain import PlayLimits, over_line_matches
from predictor.services.advisor.llm.protocol import LlmCompletion
from predictor.services.advisor.sql import latest_over_under
from predictor.services.advisor.tick import (
    BASELINE_NAME,
    LLM_NAME,
    CandidateWindow,
    run_tick,
)

NOW = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)


class _Provider:
    provider_name = "google"
    model = "gemini-2.5-flash"

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, prompt: str, *, timeout: float) -> LlmCompletion:
        self.calls += 1
        return LlmCompletion(
            text="{}",
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
        window.ready(signature, NOW + timedelta(seconds=40), timedelta(seconds=20))
        is False
    )
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


async def _drop_strategies(session) -> None:
    rows = (
        await session.scalars(
            select(AdvisorStrategy).where(
                AdvisorStrategy.name.in_((BASELINE_NAME, LLM_NAME))
            )
        )
    ).all()
    for row in rows:
        await session.execute(
            delete(AdvisorStrategyVersion).where(
                AdvisorStrategyVersion.strategy_id == row.id
            )
        )
        await session.execute(
            delete(AdvisorStrategy).where(AdvisorStrategy.id == row.id)
        )
