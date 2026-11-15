"""Recommendation cycle as a short graph. Sandbox commit happens before any wait.

Nodes do not keep a database session. The checkpoint row is the only memory
across a process restart. The cycle row is never updated.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, TypedDict, cast

from langgraph.graph import END, StateGraph

from predictor.telemetry import start_span

INTERRUPT_AFTER = timedelta(seconds=180)


class CycleGraphState(TypedDict):
    mode: str
    recommend: bool
    validation_status: str
    decision: str
    interrupt: bool
    node: str
    trail: tuple[str, ...]


def recommendation_waits(
    *,
    recommend: bool,
    validation_status: str,
    decision: str,
    has_slip: bool,
) -> bool:
    """A recommendation bet waits before its coupon exists. Sandbox does not."""
    return recommend and validation_status == "ok" and decision == "bet" and has_slip


def interrupt_closed(
    asked_at: datetime,
    now: datetime,
    *,
    score_changed: bool,
) -> bool:
    """The recommendation wait ends after 180s or when the score or period moved."""
    if score_changed:
        return True
    if asked_at.tzinfo is None or now.tzinfo is None:
        return True
    return now - asked_at > INTERRUPT_AFTER


def run_graph(state: CycleGraphState) -> CycleGraphState:
    """Walk the nodes once. Returns the state, including the node trail."""
    graph = _builder().compile()
    return cast(CycleGraphState, graph.invoke(state))


def _builder() -> StateGraph[CycleGraphState]:
    builder = StateGraph(CycleGraphState)
    for name in ("prepare", "decide", "validate", "commit"):
        builder.add_node(name, _mark(name))
    builder.add_node("interrupt", _mark("interrupt"))
    builder.set_entry_point("prepare")
    builder.add_edge("prepare", "decide")
    builder.add_edge("decide", "validate")
    builder.add_edge("validate", "commit")
    builder.add_conditional_edges(
        "commit",
        _after_commit,
        {"interrupt": "interrupt", "end": END},
    )
    builder.add_edge("interrupt", END)
    return builder


def _mark(name: str) -> Any:
    def step(state: CycleGraphState) -> dict[str, Any]:
        with start_span(name):
            trail = tuple(state.get("trail", ())) + (name,)
            return {"node": name, "trail": trail}

    return step


def _after_commit(state: CycleGraphState) -> str:
    if state["interrupt"]:
        return "interrupt"
    return "end"


def decision_label(validation_status: str, decisions: tuple[str, ...]) -> str:
    """One label for the checkpoint. An invalid reply is not a bet."""
    if validation_status != "ok":
        return "invalid"
    if "bet" in decisions:
        return "bet"
    if "wait" in decisions:
        return "wait"
    return "reject"
