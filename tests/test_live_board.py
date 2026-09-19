from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from html import escape
from typing import Any

from predictor.services.live_board import (
    LiveMatch,
    LiveScorer,
    LiveStats,
    NextGoalOdds,
    PrematchOdds,
    TeamGoalForm,
    clock_label,
    event_kind,
    format_odd,
    goal_clock_minute,
    is_fulltime_1x2_market,
    is_match_winner_market,
    market_is_next_goal,
    match_winner_side,
    next_goal_target,
    odd_side,
    parse_live_fixture_ids,
    pick_venue,
    render_live_html,
    safe_http_url,
    scoring_team_id,
    select_live_1x2,
    select_next_goal_odds,
    select_prematch_odds,
    summarize_team_goal_form,
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


def test_clock_label_ht_and_extra() -> None:
    assert clock_label("HT", 45, None) == "HT"
    assert clock_label("2H", 45, 2) == "45+2'"
    assert clock_label("1H", 12, None) == "12'"
    assert clock_label("LIVE", None, None) == "LIVE"


def test_safe_http_url_rejects_javascript() -> None:
    assert safe_http_url("javascript:alert(1)") is None
    assert (
        safe_http_url("https://example.test/logo.png")
        == "https://example.test/logo.png"
    )
    assert safe_http_url("  http://cdn.example/a.png  ") == "http://cdn.example/a.png"


def test_pick_venue_ignores_zero_id() -> None:
    assert pick_venue(0, None, None, "Wrong Stadium", "Nagoya") == (None, None)
    assert pick_venue(12, "Stadion", "Trinec", "Other", "X") == ("Stadion", "Trinec")
    assert pick_venue(12, None, None, "Bobur Arena", "Andijan") == (
        "Bobur Arena",
        "Andijan",
    )


def test_event_kind_maps_api_football_details() -> None:
    assert event_kind("Goal", "Normal Goal") == "goal"
    assert event_kind("Goal", "Penalty") == "penalty"
    assert event_kind("Goal", "Own Goal") == "own_goal"
    assert event_kind("Goal", "Missed Penalty") is None
    assert event_kind("Card", "Red Card") == "red"
    assert event_kind("Card", "Second Yellow") == "red"
    assert event_kind("Card", "Yellow Card") is None
    assert event_kind("subst", "Substitution 1") is None


def test_goal_form_uses_scored_goals_and_clock_minutes() -> None:
    assert goal_clock_minute(45, 2) == 47
    assert scoring_team_id(10, 10, 20, "goal") == 10
    assert scoring_team_id(10, 10, 20, "own_goal") == 20
    assert scoring_team_id(20, 10, 20, "own_goal") == 10
    assert scoring_team_id(10, 10, 20, None) is None
    form = summarize_team_goal_form([2, 1, 0, 3, 2], [12, 45, 47])
    assert form is not None
    assert form.matches == 5
    assert form.avg_goals == 1.6
    assert form.avg_minute == (12 + 45 + 47) / 3
    assert form.minute_label == "35'"
    assert summarize_team_goal_form([None, None], []) is None
    blank = summarize_team_goal_form([0, 0], [])
    assert blank is not None
    assert blank.avg_goals == 0
    assert blank.avg_minute is None
    assert blank.minute_label is None


def test_next_goal_target_uses_score_and_period() -> None:
    assert next_goal_target("1H", 0, 0, None, None) == (1, False)
    assert next_goal_target("2H", 2, 2, None, None) == (5, False)
    assert next_goal_target("ET", 2, 2, 0, 0) == (1, True)
    assert next_goal_target("ET", 2, 2, 1, 0) == (2, True)
    assert next_goal_target("PEN", 2, 2, 1, 1) is None


def test_market_is_next_goal_rejects_extra_time_in_regular() -> None:
    assert market_is_next_goal("Which team will score the 5th goal?", 5, False)
    assert not market_is_next_goal(
        "Which team will score the 1st goal in extra time?", 5, False
    )
    assert market_is_next_goal(
        "Which team will score the 1st goal in extra time?", 1, True
    )
    assert market_is_next_goal("Next Goal", 1, False)
    assert not market_is_next_goal("Next Goal Scorer", 1, False)


def test_odd_side_and_format() -> None:
    assert odd_side("1") == "home"
    assert odd_side("2") == "away"
    assert odd_side("No goal") == "none"
    assert odd_side("Home") == "home"
    assert format_odd(Decimal("4.750")) == "4.75"
    assert format_odd(Decimal("1.909")) == "1.909"
    assert format_odd(Decimal("2.000")) == "2"


def test_match_winner_side_and_market() -> None:
    assert is_match_winner_market("Match Winner")
    assert not is_match_winner_market("Corners 1x2")
    assert match_winner_side("Home") == "home"
    assert match_winner_side("Draw") == "draw"
    assert match_winner_side("X") == "draw"
    assert match_winner_side("Away") == "away"


def test_select_prematch_odds_keeps_one_bookmaker() -> None:
    rows = [
        (8, "Other", "Match Winner", "Home", Decimal("9.000")),
        (8, "Other", "Match Winner", "Draw", Decimal("4.000")),
        (8, "Other", "Match Winner", "Away", Decimal("1.200")),
        (1, "Bet365", "Match Winner", "Home", Decimal("1.730")),
        (1, "Bet365", "Match Winner", "Draw", Decimal("3.500")),
        (1, "Bet365", "Match Winner", "Away", Decimal("5.000")),
        (1, "Bet365", "Home/Away", "Home", Decimal("1.400")),
    ]
    quote = select_prematch_odds(rows)
    assert quote is not None
    assert quote.home == "1.73"
    assert quote.draw == "3.5"
    assert quote.away == "5"
    assert quote.bookmaker == "Bet365"
    assert quote.market == "Match Winner"


def test_select_prematch_odds_falls_back_to_home_away() -> None:
    rows = [
        (2, "Pinnacle", "Home/Away", "Home", Decimal("1.80")),
        (2, "Pinnacle", "Home/Away", "Away", Decimal("2.10")),
    ]
    quote = select_prematch_odds(rows)
    assert quote is not None
    assert quote.home == "1.8"
    assert quote.draw is None
    assert quote.away == "2.1"
    assert quote.market == "Home/Away"


def test_select_live_1x2_uses_fulltime_result() -> None:
    assert is_fulltime_1x2_market("Fulltime Result")
    assert is_fulltime_1x2_market("1x2")
    assert not is_fulltime_1x2_market("1x2 Extra Time")
    quote = select_live_1x2(
        [
            ("1x2 Extra Time", "Home", Decimal("2.0")),
            ("Fulltime Result", "Home", Decimal("1.730")),
            ("Fulltime Result", "Draw", Decimal("3.500")),
            ("Fulltime Result", "Away", Decimal("5.000")),
        ]
    )
    assert quote is not None
    assert quote.source == "live"
    assert quote.home == "1.73"
    assert quote.draw == "3.5"
    assert quote.away == "5"
    assert quote.market == "Fulltime Result"


def test_select_next_goal_odds_picks_current_ordinal() -> None:
    rows = [
        (
            "Which team will score the 1st goal in extra time?",
            "1",
            Decimal("1.9"),
            False,
        ),
        ("Which team will score the 5th goal?", "1", Decimal("4.000"), False),
        ("Which team will score the 5th goal?", "No goal", Decimal("1.285"), False),
        ("Which team will score the 5th goal?", "2", Decimal("11.000"), False),
    ]
    quote = select_next_goal_odds(rows, "2H", 2, 2, None, None)
    assert quote is not None
    assert quote.home == "4"
    assert quote.none == "1.285"
    assert quote.away == "11"
    assert "5th goal" in quote.market
    assert "extra time" not in quote.market


def test_render_escapes_team_names() -> None:
    html = render_live_html(
        [_match(home="<script>x</script>", away="Y")],
        generated_at=datetime(2026, 9, 13, 20, 0, tzinfo=UTC),
    )
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert "Mecze na żywo" in html
    assert "1 mecz" in html
    assert escape("12'") in html


def test_render_empty_state_and_grouping() -> None:
    empty = render_live_html([], generated_at=datetime(2026, 9, 13, 20, 0, tzinfo=UTC))
    assert "Żaden mecz nie jest teraz w grze" in empty
    html = render_live_html(
        [
            _match(fixture_id=1, league="Premier League"),
            _match(
                fixture_id=2,
                country="Spain",
                league="La Liga",
                home="Barça",
                away="Madrid",
                goals_home=0,
                goals_away=0,
                status_short="HT",
                elapsed=45,
            ),
        ],
        generated_at=datetime(2026, 9, 13, 20, 0, tzinfo=UTC),
    )
    assert "England · Premier League" in html
    assert "Spain · La Liga" in html
    assert "2 mecze" in html
    assert html.index("Premier League") < html.index("La Liga")


def test_render_match_facts_from_database_fields() -> None:
    html = render_live_html(
        [
            _match(
                status_short="2H",
                elapsed=69,
                ht_home=1,
                ht_away=0,
                venue="Stadion Rudolfa Labaje",
                venue_city="Trinec",
                home_reds=1,
                away_reds=0,
                home_formation="4-3-3",
                scorers=(
                    LiveScorer("home", "Saka", 12, None, "goal"),
                    LiveScorer("away", "Palmer<script>", 45, 1, "penalty"),
                ),
                stats=LiveStats(
                    possession_home="58%",
                    possession_away="42%",
                    shots_on_home="4",
                    shots_on_away="2",
                ),
                next_goal=NextGoalOdds(
                    home="1.83",
                    none="4.333",
                    away="2.88",
                    market="Which team will score the 2nd goal?",
                ),
                prematch=PrematchOdds(
                    home="1.73",
                    draw="3.5",
                    away="5",
                    market="Match Winner",
                    bookmaker="Bet365",
                ),
                form_home=TeamGoalForm(5, 1.6, 38),
                form_away=TeamGoalForm(3, 0.8, 61),
            )
        ],
        generated_at=datetime(2026, 9, 13, 20, 0, tzinfo=UTC),
    )
    assert "HT 1–0" in html
    assert "Stadion Rudolfa Labaje, Trinec" in html
    assert "4-3-3" in html
    assert "Saka" in html
    assert escape("12'") in html
    assert "Palmer&lt;script&gt;" in html
    assert "(k.)" in html
    assert escape("45+1'") in html
    assert "redcard" in html
    assert "Pos. 58%–42%" in html
    assert "Celne 4–2" in html
    assert "Następna bramka" in html
    assert "Przed meczem" in html
    assert "Remis" in html
    assert "1.73" in html
    assert "1.83" in html
    assert "Brak" in html
    assert "Ostatnie 5" in html
    assert "1.6 gola" in html
    assert escape("38'") in html
    assert "0.8 gola" in html
    assert "3 m." in html
    assert "HT 1–0" in html
    assert "1H" not in html


def test_render_skips_missing_enrichment() -> None:
    html = render_live_html(
        [_match(status_short="1H", ht_home=0, ht_away=0)],
        generated_at=datetime(2026, 9, 13, 20, 0, tzinfo=UTC),
    )
    assert "HT 0–0" not in html
    assert "Następna bramka" not in html
    assert "Przed meczem" not in html
    assert "Pos." not in html
    assert "Ostatnie 5" not in html
    assert "Regular Season - 4" in html


def test_parse_live_fixture_ids_from_live_all_task() -> None:
    assert parse_live_fixture_ids(None) == []
    assert parse_live_fixture_ids({"live": "all"}) == []
    assert parse_live_fixture_ids({"fixture_ids": ["a", 12, 12, 7]}) == [12, 7]


def test_live_match_json_includes_board_fields() -> None:
    payload = _match(
        next_goal=NextGoalOdds(
            "1.8", "4.3", "2.9", "Which team will score the 2nd goal?"
        ),
        prematch=PrematchOdds("1.73", "3.5", "5", "Match Winner", "Bet365"),
        form_home=TeamGoalForm(5, 1.6, 38.4),
    ).as_dict()
    assert payload["clock"] == "12'"
    assert payload["next_goal"]["home"] == "1.8"
    assert payload["prematch"]["draw"] == "3.5"
    assert payload["prematch"]["source"] == "prematch"
    assert payload["scorers"] == []
    assert payload["form_home"] == {
        "matches": 5,
        "avg_goals": 1.6,
        "avg_minute": 38.4,
    }
    assert payload["form_away"] is None
