"""Pure cases for this layer. They do not open Postgres."""

from __future__ import annotations

from decimal import Decimal

from predictor.services.advisor.tick import board_snapshots
from predictor.services.live_board.domain import LiveMatch, PrematchOdds


def _live(**overrides: object) -> LiveMatch:
    base = dict(
        fixture_id=1,
        country="c",
        league="l",
        round=None,
        home="H",
        away="A",
        home_logo=None,
        away_logo=None,
        goals_home=1,
        goals_away=1,
        status_short="2H",
        status_long=None,
        elapsed=60,
        extra=None,
        home_team_id=10,
        away_team_id=20,
        prediction_winner_team_id=10,
        prematch=PrematchOdds("1.50", "4.0", "6.0", "Match Winner", "Bet365"),
    )
    base.update(overrides)
    return LiveMatch(**base)


def test_strategies_share_the_board_list() -> None:
    drawing = _live(fixture_id=2)
    losing = _live(fixture_id=3, goals_home=0, goals_away=1, elapsed=70)
    two_down = _live(fixture_id=4, goals_home=0, goals_away=2, elapsed=70, away_reds=1)
    selected = board_snapshots((drawing, losing, two_down))
    assert [item.fixture_id for item in selected] == [3]
    assert [item.rule for item in selected] == ["favorite_losing"]
    assert selected[0].favorite_odd == Decimal("1.50")
