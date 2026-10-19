"""Turn a board candidate into a playable Over N+0.5 leg. No I/O."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from predictor.services.advisor.cycle import CycleCandidate
from predictor.services.advisor.domain import (
    MIN_ANALYSIS_ODD,
    CandidateView,
    LineQuote,
    Period,
    PlayLimits,
    assess,
    over_line_matches,
    period_for,
)
from predictor.services.advisor.sql import OverUnderRow

StateKey = tuple[int, str, int, int, int]


@dataclass(frozen=True, slots=True)
class MatchSnapshot:
    fixture_id: int
    status_short: str
    elapsed: int | None
    extra: int | None
    goals_home: int | None
    goals_away: int | None
    extratime_home: int | None
    extratime_away: int | None
    rule: str
    league_id: int = 0


def candidate_signature(
    candidates: Sequence[CycleCandidate],
) -> tuple[StateKey, ...]:
    return tuple(
        sorted(
            (
                item.fixture_id,
                item.period,
                item.goals_home,
                item.goals_away,
                item.ordinal,
            )
            for item in candidates
        )
    )


def playable_candidate(
    match: MatchSnapshot,
    rows: Sequence[OverUnderRow],
    *,
    limits: PlayLimits,
    now: datetime,
    open_fixtures: frozenset[int],
    rejected: frozenset[StateKey],
) -> CycleCandidate | None:
    period = period_for(match.status_short)
    quote = _quote(rows, match) if period is Period.REGULATION else None
    known = period is Period.REGULATION
    view = CandidateView(
        fixture_id=match.fixture_id,
        status_short=match.status_short,
        elapsed=match.elapsed,
        extra=match.extra,
        goals_home=match.goals_home,
        goals_away=match.goals_away,
        extratime_home=match.extratime_home,
        extratime_away=match.extratime_away,
        quote=quote,
        market_known=known,
        in_open_coupon=match.fixture_id in open_fixtures,
        state_rejected=False,
    )
    play = assess(view, limits, now)
    if (
        not play.playable
        or play.period is None
        or play.n is None
        or play.state is None
        or quote is None
        or quote.odd is None
        or quote.odd < MIN_ANALYSIS_ODD
    ):
        return None
    key = (
        play.state.fixture_id,
        play.state.period.value,
        play.state.goals_home,
        play.state.goals_away,
        play.state.ordinal,
    )
    if key in rejected or match.fixture_id in open_fixtures:
        return None
    return CycleCandidate(
        fixture_id=match.fixture_id,
        period=play.state.period.value,
        goals_home=play.state.goals_home,
        goals_away=play.state.goals_away,
        ordinal=play.state.ordinal,
        candidate_rule=match.rule,
        odd=quote.odd,
        n=play.n,
    )


def _quote(rows: Sequence[OverUnderRow], match: MatchSnapshot) -> LineQuote | None:
    goals = (match.goals_home or 0) + (match.goals_away or 0)
    for row in rows:
        if not over_line_matches(row.label, row.handicap, goals):
            continue
        return LineQuote(
            odd=row.odd, tradable=row.tradable, captured_at=row.captured_at
        )
    return None
