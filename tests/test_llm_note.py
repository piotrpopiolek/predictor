"""LLM prompt gaps shown on the next-goal list and the match page."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from predictor.services.advisor.candidates import MatchSnapshot
from predictor.services.advisor.domain import PlayLimits
from predictor.services.advisor.llm_note import (
    LlmDecisionNote,
    LlmGap,
    LlmNote,
    llm_prompt_gaps,
)
from predictor.services.advisor.sql import OverUnderRow
from predictor.ui.llm_note import render_llm_note

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
    }
    values.update(overrides)
    return MatchSnapshot(**values)  # type: ignore[arg-type]


def _row(
    odd: str = "1.500",
    *,
    tradable: bool = True,
    age: int = 20,
    handicap: str = "1.5",
) -> OverUnderRow:
    return OverUnderRow(
        fixture_id=1,
        label="Over",
        handicap=handicap,
        odd=Decimal(odd),
        tradable=tradable,
        captured_at=NOW - timedelta(seconds=age),
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


def test_a_fresh_second_half_line_has_no_llm_gaps() -> None:
    assert _gaps(_match(), (_row(),)) == ()


def test_missing_gates_are_listed_together() -> None:
    gaps = llm_prompt_gaps(
        _match(status_short="1H", elapsed=30),
        (_row(odd="1.200", age=400),),
        is_candidate=False,
        limits=LIMITS,
        now=NOW,
        open_fixtures=frozenset({1}),
        rejected=frozenset(),
    )
    assert gaps == (
        LlmGap.NOT_CANDIDATE,
        LlmGap.NOT_SECOND_HALF,
        LlmGap.STALE,
        LlmGap.ODD_LOW,
        LlmGap.OPEN_COUPON,
    )


def test_note_renders_gaps_or_the_decision() -> None:
    blocked = render_llm_note(
        LlmNote(
            gaps=(LlmGap.NOT_SECOND_HALF, LlmGap.NO_LINE),
            minute_limit=80,
            line="1.5",
            decision=None,
        )
    )
    assert "Do LLM brakuje" in blocked
    assert "status inny niż 2H" in blocked
    assert "brak Over 1.5" in blocked
    ready = render_llm_note(
        LlmNote(
            gaps=(),
            minute_limit=80,
            line="1.5",
            decision=LlmDecisionNote(
                decision="bet",
                rationale="Sheffield press",
                goals_home=0,
                goals_away=1,
                same_state=True,
            ),
        )
    )
    assert "LLM: graj przy 0:1" in ready
    assert "Sheffield press" in ready
    paused = render_llm_note(
        LlmNote(
            gaps=(),
            minute_limit=80,
            line="1.5",
            decision=None,
            strategy_paused=True,
        )
    )
    assert "wstrzymana" in paused
    assert "W puli LLM" not in paused
