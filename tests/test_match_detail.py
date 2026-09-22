from __future__ import annotations

from datetime import UTC, datetime

from predictor.services.live_board import LiveMatch, PrematchOdds, TeamGoalForm
from predictor.services.match_detail import (
    DetailEvent,
    DetailH2H,
    DetailPlayer,
    DetailStat,
    MatchDetail,
    render_match_html,
    render_match_missing,
)


def _detail() -> MatchDetail:
    match = LiveMatch(
        fixture_id=42,
        country="Iran",
        league="Azadegan League",
        round="Regular Season - 2",
        home="Mes Kerman",
        away="Saipa",
        home_logo=None,
        away_logo=None,
        goals_home=0,
        goals_away=1,
        status_short="2H",
        status_long="Second Half",
        elapsed=90,
        extra=1,
        venue="Shahid Bahonar Stadium",
        venue_city="Kerman",
        ht_home=0,
        ht_away=1,
        home_formation="4-4-2",
        away_formation="4-3-3",
        prematch=PrematchOdds("2.10", "3.20", "3.40", "Match Winner", "Bet365"),
        form_home=TeamGoalForm(5, 0.4, 40.0),
        prediction_pct_home=45.0,
        prediction_pct_away=28.0,
    )
    return MatchDetail(
        match=match,
        events=(DetailEvent(55, None, "away", "Goal", "Normal Goal", "Saipa", None),),
        stats=(DetailStat("Posiadanie", "48%", "52%"),),
        home_xi=(DetailPlayer("Ali", 9, "F", True),),
        away_xi=(DetailPlayer("Reza", 10, "M", True),),
        home_bench=(),
        away_bench=(DetailPlayer("Omid", 17, None, False),),
        advice="Double chance : Saipa or draw",
        expected_home="1.1",
        expected_away="1.4",
        pct_draw="27%",
        comparison=(("Forma", "40%", "60%"),),
        h2h=(DetailH2H("2026-05-01", "Saipa", "Mes Kerman", "1–0"),),
    )


def test_render_match_html_shows_collected_sections() -> None:
    html = render_match_html(
        _detail(), generated_at=datetime(2026, 9, 22, 14, 0, tzinfo=UTC)
    )
    assert "Mes Kerman" in html
    assert "Saipa" in html
    assert "0–1" in html
    assert "90+1" in html
    assert "Shahid Bahonar Stadium" in html
    assert "HT 0–1" in html
    assert "Zdarzenia" in html
    assert "Normal Goal" in html
    assert "Posiadanie" in html
    assert "Składy" in html
    assert "Ali" in html
    assert "Ławka" in html
    assert "Prognoza" in html
    assert "Double chance" in html
    assert "Przed meczem" in html
    assert "Bezpośrednie" in html
    assert 'href="/live"' in html


def test_render_match_missing() -> None:
    html = render_match_missing()
    assert "Nie ma takiego meczu" in html
