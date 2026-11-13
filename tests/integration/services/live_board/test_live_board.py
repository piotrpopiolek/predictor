from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete

from predictor.models.catalog import League
from predictor.models.fixtures import Fixture, Team
from predictor.models.predictions import Prediction, PredictionH2H
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings
from predictor.services.live_board import (
    FirstLegScore,
    LiveMatch,
    PrematchOdds,
    is_favorite_losing,
    load_first_legs,
    next_goal_reasons,
)

_PAIR_LEAGUE = 9_720_001
_PAIR_HOME = 9_720_002
_PAIR_AWAY = 9_720_003
_PAIR_OTHER_LEAGUE = 9_720_004
_PAIR_MARKED_HOME = 9_720_005
_PAIR_MARKED_AWAY = 9_720_006
_PAIR_LIVE = 9_720_010
_PAIR_FIRST = 9_720_011
_PAIR_WRONG_SEASON = 9_720_012
_PAIR_WRONG_ROUND = 9_720_013
_PAIR_WRONG_LEAGUE = 9_720_014
_PAIR_FINAL_LIVE = 9_720_020
_PAIR_FINAL_FT = 9_720_021
_PAIR_MARKED_LIVE = 9_720_030
_PAIR_MARKED_FIRST = 9_720_031
_PAIR_FIXTURE_IDS = (
    _PAIR_FIRST,
    _PAIR_WRONG_SEASON,
    _PAIR_WRONG_ROUND,
    _PAIR_WRONG_LEAGUE,
    _PAIR_FINAL_FT,
    _PAIR_MARKED_FIRST,
)
_PAIR_TEAM_IDS = (
    _PAIR_HOME,
    _PAIR_AWAY,
    _PAIR_MARKED_HOME,
    _PAIR_MARKED_AWAY,
)

_SAMPLE = LiveMatch(
    fixture_id=1,
    country="England",
    league="Premier League",
    round="Regular Season - 4",
    home="Arsenal",
    away="Chelsea",
    home_logo=None,
    away_logo=None,
    goals_home=1,
    goals_away=0,
    status_short="1H",
    status_long="First Half",
    elapsed=12,
    extra=None,
)


def _match(**overrides: Any) -> LiveMatch:
    return replace(_SAMPLE, **overrides)


def _fav_home(**overrides: Any) -> LiveMatch:
    base = {
        "home_team_id": 10,
        "away_team_id": 20,
        "league_id": 39,
        "prematch": PrematchOdds("1.50", "4.0", "6.0", "Match Winner", "Bet365"),
        "goals_home": 0,
        "goals_away": 1,
        "status_short": "2H",
        "elapsed": 60,
    }
    base.update(overrides)
    return _match(**base)


async def _drop_pair_fixtures(session) -> None:
    ids = _PAIR_FIXTURE_IDS
    await session.execute(delete(Prediction).where(Prediction.fixture_id.in_(ids)))
    await session.execute(
        delete(PredictionH2H).where(
            PredictionH2H.fixture_id.in_(ids) | PredictionH2H.h2h_fixture_id.in_(ids)
        )
    )
    await session.execute(delete(Fixture).where(Fixture.id.in_(ids)))
    await session.execute(delete(Team).where(Team.id.in_(_PAIR_TEAM_IDS)))
    await session.execute(
        delete(League).where(League.id.in_((_PAIR_LEAGUE, _PAIR_OTHER_LEAGUE)))
    )


@pytest.mark.asyncio
async def test_load_first_legs_filters_season_round_league_and_final() -> None:
    """DB pairing must not invent a return leg from the wrong row."""
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    live_kickoff = datetime(2026, 5, 20, 18, 0, tzinfo=UTC)
    try:
        async with factory() as session, session.begin():
            await _drop_pair_fixtures(session)
            session.add(League(id=_PAIR_LEAGUE, name="pair-league"))
            session.add(League(id=_PAIR_OTHER_LEAGUE, name="pair-other-league"))
            session.add(Team(id=_PAIR_HOME, name="Pair Home"))
            session.add(Team(id=_PAIR_AWAY, name="Pair Away"))
            session.add(Team(id=_PAIR_MARKED_HOME, name="Marked Home"))
            session.add(Team(id=_PAIR_MARKED_AWAY, name="Marked Away"))
            await session.flush()
            session.add_all(
                [
                    Fixture(
                        id=_PAIR_FIRST,
                        league_id=_PAIR_LEAGUE,
                        season=2026,
                        round="Semi-finals",
                        home_team_id=_PAIR_AWAY,
                        away_team_id=_PAIR_HOME,
                        status_short="FT",
                        date=live_kickoff - timedelta(days=7),
                        goals_home=1,
                        goals_away=0,
                        goals_home_fulltime=1,
                        goals_away_fulltime=0,
                    ),
                    Fixture(
                        id=_PAIR_WRONG_SEASON,
                        league_id=_PAIR_LEAGUE,
                        season=2025,
                        round="Semi-finals",
                        home_team_id=_PAIR_AWAY,
                        away_team_id=_PAIR_HOME,
                        status_short="FT",
                        date=live_kickoff - timedelta(days=8),
                        goals_home=5,
                        goals_away=0,
                        goals_home_fulltime=5,
                        goals_away_fulltime=0,
                    ),
                    Fixture(
                        id=_PAIR_WRONG_ROUND,
                        league_id=_PAIR_LEAGUE,
                        season=2026,
                        round="Quarter-finals",
                        home_team_id=_PAIR_AWAY,
                        away_team_id=_PAIR_HOME,
                        status_short="FT",
                        date=live_kickoff - timedelta(days=6),
                        goals_home=4,
                        goals_away=0,
                        goals_home_fulltime=4,
                        goals_away_fulltime=0,
                    ),
                    Fixture(
                        id=_PAIR_WRONG_LEAGUE,
                        league_id=_PAIR_OTHER_LEAGUE,
                        season=2026,
                        round="Semi-finals",
                        home_team_id=_PAIR_AWAY,
                        away_team_id=_PAIR_HOME,
                        status_short="FT",
                        date=live_kickoff - timedelta(days=5),
                        goals_home=3,
                        goals_away=0,
                        goals_home_fulltime=3,
                        goals_away_fulltime=0,
                    ),
                    Fixture(
                        id=_PAIR_FINAL_FT,
                        league_id=_PAIR_LEAGUE,
                        season=2026,
                        round="Final",
                        home_team_id=_PAIR_AWAY,
                        away_team_id=_PAIR_HOME,
                        status_short="FT",
                        date=live_kickoff - timedelta(days=14),
                        goals_home=2,
                        goals_away=1,
                        goals_home_fulltime=2,
                        goals_away_fulltime=1,
                    ),
                    Fixture(
                        id=_PAIR_MARKED_FIRST,
                        league_id=_PAIR_LEAGUE,
                        season=2026,
                        round="1st Leg",
                        home_team_id=_PAIR_MARKED_HOME,
                        away_team_id=_PAIR_MARKED_AWAY,
                        status_short="FT",
                        date=live_kickoff - timedelta(days=7),
                        goals_home=0,
                        goals_away=0,
                        goals_home_fulltime=0,
                        goals_away_fulltime=0,
                    ),
                ]
            )

        semi = _fav_home(
            fixture_id=_PAIR_LIVE,
            round="Semi-finals",
            season=2026,
            league_id=_PAIR_LEAGUE,
            home_team_id=_PAIR_HOME,
            away_team_id=_PAIR_AWAY,
            kickoff=live_kickoff,
            goals_home=1,
            goals_away=0,
            prematch=PrematchOdds("5", "4.2", "1.47", "Match Winner", "Betfair"),
        )
        final = _fav_home(
            fixture_id=_PAIR_FINAL_LIVE,
            round="Final",
            season=2026,
            league_id=_PAIR_LEAGUE,
            home_team_id=_PAIR_HOME,
            away_team_id=_PAIR_AWAY,
            kickoff=live_kickoff,
        )
        marked = _fav_home(
            fixture_id=_PAIR_MARKED_LIVE,
            round="2nd Leg",
            season=2026,
            league_id=_PAIR_LEAGUE,
            home_team_id=_PAIR_MARKED_HOME,
            away_team_id=_PAIR_MARKED_AWAY,
            kickoff=live_kickoff,
            goals_home=0,
            goals_away=1,
        )

        legs = await load_first_legs(engine, [semi, final, marked])
        assert _PAIR_FINAL_LIVE not in legs
        assert legs[_PAIR_LIVE] == FirstLegScore(
            home_team_id=_PAIR_AWAY,
            away_team_id=_PAIR_HOME,
            goals_home=1,
            goals_away=0,
        )
        assert legs[_PAIR_MARKED_LIVE] == FirstLegScore(
            home_team_id=_PAIR_MARKED_HOME,
            away_team_id=_PAIR_MARKED_AWAY,
            goals_home=0,
            goals_away=0,
        )
        assert is_favorite_losing(semi) is True
        assert next_goal_reasons(semi, legs[_PAIR_LIVE]) == ()
        assert next_goal_reasons(final) == ("favorite_losing",)
        assert next_goal_reasons(marked, legs[_PAIR_MARKED_LIVE]) == ("tie_deficit",)
    finally:
        async with factory() as session, session.begin():
            await _drop_pair_fixtures(session)
        await engine.dispose()
