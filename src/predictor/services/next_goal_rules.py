"""Shared next-goal clock and G1/G2 gates. No database, clock, or HTTP.

The live board and the advisor both import this module. Neither imports the
other for the match minute or these two thresholds.
"""

from __future__ import annotations

from decimal import Decimal

# G1: goals that count inside the settlement period.
LLM_MAX_PERIOD_GOALS = 2
# G2: prematch favourite must be strictly below this odd.
LLM_MAX_FAVORITE_ODD = Decimal("2.000")


def match_minute(
    status_short: str, elapsed: int | None, extra: int | None
) -> int | None:
    """Match minute as the API clock shows it, plus stoppage.

    In extra time ``elapsed`` is already 91–120. ``clock_sort_key`` adds 90
    only so those matches sort after regulation.
    """
    added = extra or 0
    if status_short == "HT":
        base = 45 if elapsed is None else elapsed
        return base + added
    if elapsed is None:
        return None
    return elapsed + added
