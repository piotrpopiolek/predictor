"""Graph route: sandbox commits, a recommendation bet waits."""

from datetime import UTC, datetime, timedelta

from predictor.services.advisor.graph import (
    CycleGraphState,
    interrupt_closed,
    recommendation_waits,
    run_graph,
)

NOW = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)


def _state(*, interrupt: bool, decision: str = "bet") -> CycleGraphState:
    return CycleGraphState(
        mode="recommendation" if interrupt else "sandbox",
        recommend=interrupt,
        validation_status="ok",
        decision=decision,
        interrupt=interrupt,
        node="",
        trail=(),
    )


def test_sandbox_trail_ends_at_commit() -> None:
    result = run_graph(_state(interrupt=False))
    assert result["trail"] == ("prepare", "decide", "validate", "commit")
    assert result["node"] == "commit"


def test_recommendation_bet_stops_after_commit() -> None:
    result = run_graph(_state(interrupt=True))
    assert result["trail"] == ("prepare", "decide", "validate", "commit", "interrupt")
    assert result["node"] == "interrupt"


def test_recommendation_waits_only_for_a_bet_slip() -> None:
    assert recommendation_waits(
        recommend=True, validation_status="ok", decision="bet", has_slip=True
    )
    assert not recommendation_waits(
        recommend=True, validation_status="ok", decision="reject", has_slip=True
    )
    assert not recommendation_waits(
        recommend=False, validation_status="ok", decision="bet", has_slip=True
    )
    assert not recommendation_waits(
        recommend=True, validation_status="ok", decision="bet", has_slip=False
    )


def test_interrupt_closes_after_180_seconds_or_a_score_change() -> None:
    later = NOW + timedelta(seconds=180)
    assert interrupt_closed(NOW, later, score_changed=False) is False
    assert interrupt_closed(NOW, later + timedelta(seconds=1), score_changed=False)
    assert interrupt_closed(NOW, NOW + timedelta(seconds=10), score_changed=True)
