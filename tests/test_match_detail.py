from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from predictor.services.live_board import LiveMatch, PrematchOdds, TeamGoalForm
from predictor.services.match_detail import (
    DetailEvent,
    DetailH2H,
    DetailPlayer,
    DetailStat,
    MatchDetail,
    StandingSlot,
    _snapshot_includes_match,
    build_group_table,
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


def _group_b() -> list[StandingSlot]:
    return [
        StandingSlot(1, "Iran U23", 4, 2, 3, 1, 1),
        StandingSlot(2, "China PR U23", 4, 2, 2, 1, 2),
        StandingSlot(3, "Korea DPR U23", 1, 2, 1, 2, 3),
        StandingSlot(4, "UAE U23", 1, 2, 1, 3, 4),
    ]


def test_live_score_moves_the_group() -> None:
    table = build_group_table(
        group="Group B",
        rows=_group_b(),
        home_id=1,
        away_id=3,
        home_name="Iran U23",
        away_name="Korea DPR U23",
        home_goals=1,
        away_goals=2,
        status_short="2H",
        included=False,
        captured="23.09 00:00 UTC",
    )
    assert table.projected is not None
    assert [row.slot.name for row in table.before] == [
        "Iran U23",
        "China PR U23",
        "Korea DPR U23",
        "UAE U23",
    ]
    assert [row.slot.name for row in table.projected] == [
        "Iran U23",
        "China PR U23",
        "Korea DPR U23",
        "UAE U23",
    ]
    korea = table.projected[2]
    assert korea.slot.points == 4
    assert korea.slot.gd == 0
    assert korea.delta == 0
    assert table.projected[1].slot.gd == 1
    assert table.moves == "Miejsca się nie zmieniają."
    assert table.splits[2].active
    assert table.split == "Korea DPR U23 +3, Iran U23 +0"


def test_two_goal_win_reorders_the_group() -> None:
    table = build_group_table(
        group="Group B",
        rows=_group_b(),
        home_id=1,
        away_id=3,
        home_name="Iran U23",
        away_name="Korea DPR U23",
        home_goals=0,
        away_goals=2,
        status_short="2H",
        included=False,
        captured=None,
    )
    assert table.projected is not None
    assert [row.slot.name for row in table.projected] == [
        "Korea DPR U23",
        "China PR U23",
        "Iran U23",
        "UAE U23",
    ]
    deltas = {row.slot.name: row.delta for row in table.projected}
    assert deltas["Korea DPR U23"] == 2
    assert deltas["Iran U23"] == -2
    assert table.moves is not None
    assert "Korea DPR U23 awansuje na 1." in table.moves
    assert "Iran U23 spada na 3." in table.moves


def test_kickoff_shows_splits_without_a_live_table() -> None:
    table = build_group_table(
        group="Group B",
        rows=_group_b(),
        home_id=1,
        away_id=3,
        home_name="Iran U23",
        away_name="Korea DPR U23",
        home_goals=None,
        away_goals=None,
        status_short="NS",
        included=False,
        captured=None,
    )
    assert table.projected is None
    assert not any(item.active for item in table.splits)
    assert table.splits[0].home_points == 7
    assert table.splits[1].home_points == 5
    assert table.splits[2].away_points == 4


def test_finished_table_is_not_counted_twice() -> None:
    live = build_group_table(
        group="Group B",
        rows=_group_b(),
        home_id=1,
        away_id=3,
        home_name="Iran U23",
        away_name="Korea DPR U23",
        home_goals=1,
        away_goals=2,
        status_short="2H",
        included=False,
        captured=None,
    )
    assert live.projected is not None
    finished = build_group_table(
        group="Group B",
        rows=[row.slot for row in live.projected],
        home_id=1,
        away_id=3,
        home_name="Iran U23",
        away_name="Korea DPR U23",
        home_goals=1,
        away_goals=2,
        status_short="FT",
        included=True,
        captured=None,
    )
    assert [row.slot.points for row in finished.before] == [
        row.slot.points for row in live.before
    ]
    assert finished.projected is not None
    assert [row.slot.name for row in finished.projected] == [
        row.slot.name for row in live.projected
    ]


def test_snapshot_includes_only_a_finished_match() -> None:
    others = {1: 2, 3: 2}
    assert not _snapshot_includes_match(
        status_short="2H",
        home_played=2,
        away_played=2,
        other_finished=others,
        home_id=1,
        away_id=3,
    )
    assert _snapshot_includes_match(
        status_short="FT",
        home_played=3,
        away_played=3,
        other_finished=others,
        home_id=1,
        away_id=3,
    )
    assert not _snapshot_includes_match(
        status_short="FT",
        home_played=2,
        away_played=2,
        other_finished=others,
        home_id=1,
        away_id=3,
    )


def test_render_match_html_shows_table_movement() -> None:
    table = build_group_table(
        group="Group B",
        rows=_group_b(),
        home_id=1,
        away_id=3,
        home_name="Iran U23",
        away_name="Korea DPR U23",
        home_goals=0,
        away_goals=2,
        status_short="2H",
        included=False,
        captured="23.09 00:00 UTC",
    )
    detail = replace(
        _detail(),
        match=replace(
            _detail().match,
            home="Iran U23",
            away="Korea DPR U23",
            home_team_id=1,
            away_team_id=3,
            goals_home=0,
            goals_away=2,
            status_short="2H",
        ),
        table=table,
    )
    html = render_match_html(
        detail, generated_at=datetime(2026, 9, 23, 6, 40, tzinfo=UTC)
    )
    assert "Tabela" in html
    assert "Przed meczem" in html
    assert "Przy wyniku 0–2" in html
    assert "Korea DPR U23 awansuje na 1." in html
    assert "Iran U23 spada na 3." in html
    assert 'class="split-card active"' in html
