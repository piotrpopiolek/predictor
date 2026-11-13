"""LLM prompt gaps shown on the next-goal list and the match page."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from predictor.models.advisor import (
    AdvisorCycle,
    AdvisorDecision,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.models.catalog import League, OddsLiveBet
from predictor.models.fixtures import Fixture, Team
from predictor.models.odds import FixtureOddsLive
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings
from predictor.services.advisor.candidates import MatchSnapshot
from predictor.services.advisor.domain import PlayLimits
from predictor.services.advisor.llm_note import (
    LlmGap,
    llm_prompt_gaps,
    load_llm_notes,
)
from predictor.services.advisor.sql import OverUnderRow
from predictor.services.advisor.tick import restore_window
from predictor.services.live_board.domain import LiveMatch

NOW = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
LIMITS = PlayLimits(
    max_minute_regulation=80,
    max_minute_extra=110,
    max_quote_age=timedelta(seconds=300),
)


def _match(**overrides: object) -> MatchSnapshot:
    values: dict[str, object] = {
        "fixture_id": 1,
        "status_short": "2H",
        "elapsed": 60,
        "extra": None,
        "goals_home": 0,
        "goals_away": 1,
        "extratime_home": None,
        "extratime_away": None,
        "rule": "",
        "favorite_odd": Decimal("1.50"),
    }
    values.update(overrides)
    return MatchSnapshot(**values)  # type: ignore[arg-type]


def _row(
    odd: str = "1.500",
    *,
    tradable: bool = True,
    age: int = 20,
    handicap: str = "1.5",
    home_goals: int | None = 0,
    away_goals: int | None = 1,
) -> OverUnderRow:
    return OverUnderRow(
        fixture_id=1,
        label="Over",
        handicap=handicap,
        odd=Decimal(odd),
        tradable=tradable,
        captured_at=NOW - timedelta(seconds=age),
        home_goals=home_goals,
        away_goals=away_goals,
    )


def _gaps(
    match: MatchSnapshot, rows: tuple[OverUnderRow, ...] = ()
) -> tuple[LlmGap, ...]:
    return llm_prompt_gaps(
        match,
        rows,
        is_candidate=True,
        limits=LIMITS,
        now=NOW,
        open_fixtures=frozenset(),
        rejected=frozenset(),
    )


_LEAGUE = 889_201
_HOME = 889_202
_AWAY = 889_203
_FIXTURE = 889_204
_BET = 889_208


def _live() -> LiveMatch:
    return LiveMatch(
        fixture_id=_FIXTURE,
        country="PL",
        league="Note",
        round=None,
        home="Home",
        away="Away",
        home_logo=None,
        away_logo=None,
        goals_home=0,
        goals_away=1,
        status_short="2H",
        status_long="Second Half",
        elapsed=60,
        extra=None,
        home_team_id=_HOME,
        away_team_id=_AWAY,
        league_id=_LEAGUE,
    )


def _cycle(
    version_id: int, *, status: str, raw: str, body: dict[str, object]
) -> AdvisorCycle:
    return AdvisorCycle(
        strategy_version_id=version_id,
        mode="sandbox",
        provider="google",
        model="gemini",
        model_version="gemini",
        prompt_version="p4",
        prompt_text="prompt",
        input_json=body,
        raw_output=raw,
        validation_status=status,
        input_tokens=1,
        output_tokens=1,
        thoughts_tokens=0,
        cost_pln=Decimal("0.0100"),
        started_at=NOW,
    )


@pytest.mark.asyncio
async def test_notes_follow_the_llm_strategy_and_a_low_coupon_reopens() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    raw = json.dumps(
        {
            "candidates": [
                {
                    "fixture_id": _FIXTURE,
                    "decision": "bet",
                    "estimated_probability": "0.70",
                    "confidence": "0.60",
                    "reasons": ["favorite_pressure"],
                    "rationale": "chasing",
                }
            ],
            "coupons": [{"fixture_ids": [_FIXTURE]}],
        }
    )
    body = {
        "candidates": [
            {
                "fixture_id": _FIXTURE,
                "period": "regulation",
                "goals_home": 0,
                "goals_away": 1,
                "ordinal": 2,
                "odd": "1.350",
            }
        ]
    }
    strategy_id = 0
    version_id = 0
    try:
        assert await load_llm_notes(engine, []) == {}
        await load_llm_notes(engine, [_live()], candidate_ids={_FIXTURE}, now=NOW)
        async with factory() as session, session.begin():
            session.add(League(id=_LEAGUE, name="llm-note"))
            session.add(Team(id=_HOME, name="NoteHome"))
            session.add(Team(id=_AWAY, name="NoteAway"))
            await session.flush()
            session.add(
                Fixture(
                    id=_FIXTURE,
                    league_id=_LEAGUE,
                    season=2026,
                    home_team_id=_HOME,
                    away_team_id=_AWAY,
                    status_short="2H",
                    elapsed_minutes=60,
                    goals_home=0,
                    goals_away=1,
                )
            )
            session.add(OddsLiveBet(id=_BET, name="Over/Under Line"))
            strategy = AdvisorStrategy(
                name="advisor-llm-note", kind="llm", status="paused"
            )
            session.add(strategy)
            await session.flush()
            strategy_id = strategy.id
            version = AdvisorStrategyVersion(
                strategy_id=strategy.id,
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
            session.add(version)
            await session.flush()
            version_id = version.id
            session.add(
                FixtureOddsLive(
                    fixture_id=_FIXTURE,
                    bet_id=_BET,
                    value_label="Over",
                    handicap="1.5",
                    odd=Decimal("1.500"),
                    captured_at=NOW - timedelta(seconds=20),
                )
            )
            session.add(
                AdvisorDecision(
                    strategy_id=strategy.id,
                    fixture_id=_FIXTURE,
                    period="regulation",
                    goals_home=0,
                    goals_away=1,
                    ordinal=2,
                    candidate_rule="favorite_losing",
                    decision="bet",
                    reasons=["favorite_pressure"],
                    rationale="chasing",
                    n=1,
                    created_at=NOW,
                )
            )
            session.add(_cycle(version.id, status="invalid_output", raw=raw, body=body))
        notes = await load_llm_notes(
            engine, [_live()], candidate_ids={_FIXTURE}, now=NOW
        )
        note = notes[_FIXTURE]
        assert note.as_dict()["line"] == "1.5"
        async with factory() as session:
            active = await session.scalar(
                select(AdvisorStrategy.id).where(
                    AdvisorStrategy.kind == "llm",
                    AdvisorStrategy.status.in_(("sandbox", "promoted")),
                    AdvisorStrategy.id != strategy_id,
                )
            )
        if active is None:
            assert note.strategy_paused is True
        async with factory() as session, session.begin():
            window = await restore_window(session, strategy_id)
        assert window.fired is False
        async with factory() as session, session.begin():
            session.add(_cycle(version_id, status="ok", raw="{}", body=body))
            strategy = await session.get(AdvisorStrategy, strategy_id)
            assert strategy is not None
            strategy.status = "sandbox"
        notes = await load_llm_notes(engine, [_live()], candidate_ids=None, now=NOW)
        if active is None:
            assert notes[_FIXTURE].strategy_paused is False
        async with factory() as session, session.begin():
            window = await restore_window(session, strategy_id)
        assert window.fired is True
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                delete(AdvisorDecision).where(
                    AdvisorDecision.strategy_id == strategy_id
                )
            )
            await session.execute(
                delete(AdvisorCycle).where(
                    AdvisorCycle.strategy_version_id == version_id
                )
            )
            await session.execute(
                delete(AdvisorStrategyVersion).where(
                    AdvisorStrategyVersion.id == version_id
                )
            )
            await session.execute(
                delete(AdvisorStrategy).where(AdvisorStrategy.id == strategy_id)
            )
            await session.execute(
                delete(FixtureOddsLive).where(FixtureOddsLive.fixture_id == _FIXTURE)
            )
            await session.execute(delete(OddsLiveBet).where(OddsLiveBet.id == _BET))
            await session.execute(delete(Fixture).where(Fixture.id == _FIXTURE))
            await session.execute(delete(Team).where(Team.id.in_((_HOME, _AWAY))))
            await session.execute(delete(League).where(League.id == _LEAGUE))
        await engine.dispose()
