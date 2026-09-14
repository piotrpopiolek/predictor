from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from html import escape
from typing import Any

from predictor.services.live_board import (
    LiveMatch,
    clock_label,
    parse_live_fixture_ids,
    render_live_html,
    safe_http_url,
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


def test_parse_live_fixture_ids_from_live_all_task() -> None:
    assert parse_live_fixture_ids(None) == []
    assert parse_live_fixture_ids({"live": "all"}) == []
    assert parse_live_fixture_ids({"fixture_ids": ["a", 12, 12, 7]}) == [12, 7]
