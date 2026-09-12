"""Match next-goal markets by live-bet *name*, never by /odds/bets ids (FR-017)."""

from __future__ import annotations

import re

_EXACT_NAMES = frozenset({"next goal", "goal next"})
_ORDINAL_GOAL = re.compile(
    r"^which team will score the \d+(st|nd|rd|th) goal(?: in extra time)?\??$"
)


def normalized_bet_name(name: str) -> str:
    return " ".join(name.casefold().split())


def is_next_goal_market(name: str) -> bool:
    folded = normalized_bet_name(name)
    if "scorer" in folded or "player" in folded:
        return False
    if folded in _EXACT_NAMES:
        return True
    return _ORDINAL_GOAL.fullmatch(folded) is not None


def next_goal_matches(bets: list[tuple[int, str | None]]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    seen: set[int] = set()
    for bet_id, name in bets:
        if not name or not is_next_goal_market(name):
            continue
        if bet_id in seen:
            continue
        seen.add(bet_id)
        found.append((bet_id, name))
    return found
