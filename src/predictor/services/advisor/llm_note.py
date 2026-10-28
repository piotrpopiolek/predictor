"""Why a live match is outside the LLM prompt, or the latest decision."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from predictor.models.advisor import (
    AdvisorDecision,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.services.advisor.candidates import (
    MatchSnapshot,
    lone_candidate_allowed,
    quote_for_period,
)
from predictor.services.advisor.cycle import CycleCandidate
from predictor.services.advisor.domain import (
    LLM_STATUSES,
    MIN_ANALYSIS_ODD,
    QUOTE_MAX_AGE,
    LineQuote,
    Period,
    PlayLimits,
    match_minute,
    over_half_line,
    period_for,
    prompt_gate_misses,
)
from predictor.services.advisor.sql import (
    OverUnderRow,
    latest_over_under,
    open_fixture_ids,
    open_sandbox_coupon_labels,
    rejected_states,
)
from predictor.services.live_board.domain import (
    LiveMatch,
    favorite_prematch_odd,
    next_goal_reasons,
)
from predictor.services.live_board.sql import list_next_goal_matches, load_first_legs

_ACTIVE = frozenset({"sandbox", "promoted"})
_DEFAULT_MINUTE = 80


class LlmGap(StrEnum):
    NOT_CANDIDATE = "not_candidate"
    NOT_SECOND_HALF = "not_2h"
    MINUTE = "minute"
    NO_LINE = "no_line"
    SUSPENDED = "suspended"
    STALE = "stale"
    ODD_LOW = "odd_low"
    PERIOD_GOALS = "period_goals"
    FAVORITE_ODD = "favorite_odd"
    SHORT_LATE = "short_late"
    LONE_WEAK = "lone_weak"
    OPEN_COUPON = "open_coupon"
    REJECTED = "rejected"
    PENALTIES = "penalties"
    NOT_IN_PLAY = "not_in_play"


@dataclass(frozen=True, slots=True)
class LlmDecisionNote:
    decision: str
    rationale: str
    goals_home: int
    goals_away: int
    same_state: bool


@dataclass(frozen=True, slots=True)
class LlmNote:
    gaps: tuple[LlmGap, ...]
    minute_limit: int
    line: str | None
    decision: LlmDecisionNote | None
    strategy_paused: bool = False

    def as_dict(self) -> dict[str, object]:
        decision = None
        if self.decision is not None:
            decision = {
                "decision": self.decision.decision,
                "rationale": self.decision.rationale,
                "goals_home": self.decision.goals_home,
                "goals_away": self.decision.goals_away,
                "same_state": self.decision.same_state,
            }
        return {
            "gaps": [item.value for item in self.gaps],
            "minute_limit": self.minute_limit,
            "line": self.line,
            "decision": decision,
            "strategy_paused": self.strategy_paused,
        }


def llm_prompt_gaps(
    match: MatchSnapshot,
    rows: Sequence[OverUnderRow],
    *,
    is_candidate: bool,
    limits: PlayLimits,
    now: datetime,
    open_fixtures: frozenset[int],
    rejected: frozenset[tuple[int, str, int, int, int]],
) -> tuple[LlmGap, ...]:
    """Every LLM prompt gate this match currently fails."""
    gaps: list[LlmGap] = []
    if not is_candidate:
        gaps.append(LlmGap.NOT_CANDIDATE)
    if match.status_short == "P":
        gaps.append(LlmGap.PENALTIES)
    period = period_for(match.status_short)
    if match.status_short not in LLM_STATUSES:
        if match.status_short != "P":
            gaps.append(LlmGap.NOT_SECOND_HALF)
    else:
        minute = match_minute(match.status_short, match.elapsed, match.extra)
        limit = (
            limits.max_minute_extra
            if period is Period.EXTRA_TIME
            else limits.max_minute_regulation
        )
        if minute is None or minute > limit:
            gaps.append(LlmGap.MINUTE)
    if (
        period is None
        and LlmGap.PENALTIES not in gaps
        and LlmGap.NOT_SECOND_HALF not in gaps
    ):
        gaps.append(LlmGap.NOT_IN_PLAY)
    quote = None
    if period is not None:
        quote = quote_for_period(
            rows,
            period=period,
            goals_home=match.goals_home,
            goals_away=match.goals_away,
            extratime_home=match.extratime_home,
            extratime_away=match.extratime_away,
        )
    if quote is None or quote.odd is None or quote.odd <= 1:
        gaps.append(LlmGap.NO_LINE)
    else:
        if not quote.tradable:
            gaps.append(LlmGap.SUSPENDED)
        if _quote_is_stale(quote, limits, now):
            gaps.append(LlmGap.STALE)
        if quote.odd < MIN_ANALYSIS_ODD:
            gaps.append(LlmGap.ODD_LOW)
        if period is not None:
            n = _period_goals(match, period)
            if n is not None:
                for miss in prompt_gate_misses(
                    period=period,
                    n=n,
                    minute=match_minute(match.status_short, match.elapsed, match.extra),
                    odd=quote.odd,
                    favorite_odd=match.favorite_odd,
                ):
                    gaps.append(LlmGap(miss.value))
    if match.fixture_id in open_fixtures:
        gaps.append(LlmGap.OPEN_COUPON)
    if _state_rejected(match, rejected):
        gaps.append(LlmGap.REJECTED)
    return tuple(gaps)


def _period_goals(match: MatchSnapshot, period: Period) -> int | None:
    if period is Period.EXTRA_TIME:
        return (match.extratime_home or 0) + (match.extratime_away or 0)
    if match.goals_home is None or match.goals_away is None:
        return None
    return match.goals_home + match.goals_away


def _quote_is_stale(quote: LineQuote, limits: PlayLimits, now: datetime) -> bool:
    captured = quote.captured_at
    if captured is None or captured.tzinfo is None or now.tzinfo is None:
        return True
    age = now - captured
    return age.total_seconds() < 0 or age > limits.max_quote_age


def _state_rejected(
    match: MatchSnapshot,
    rejected: frozenset[tuple[int, str, int, int, int]],
) -> bool:
    if match.goals_home is None or match.goals_away is None:
        return False
    period = period_for(match.status_short)
    if period is None:
        return False
    if period is Period.EXTRA_TIME:
        n = (match.extratime_home or 0) + (match.extratime_away or 0)
    else:
        n = match.goals_home + match.goals_away
    key = (match.fixture_id, period.value, match.goals_home, match.goals_away, n + 1)
    return key in rejected


def _line_label(match: LiveMatch) -> str | None:
    period = period_for(match.status_short)
    if period is Period.EXTRA_TIME:
        return str(over_half_line((match.et_home or 0) + (match.et_away or 0)))
    if match.goals_home is None or match.goals_away is None:
        return None
    return str(over_half_line(match.goals_home + match.goals_away))


def _snapshot(match: LiveMatch) -> MatchSnapshot:
    return MatchSnapshot(
        fixture_id=match.fixture_id,
        status_short=match.status_short,
        elapsed=match.elapsed,
        extra=match.extra,
        goals_home=match.goals_home,
        goals_away=match.goals_away,
        extratime_home=match.et_home,
        extratime_away=match.et_away,
        rule="",
        league_id=match.league_id,
        favorite_odd=favorite_prematch_odd(match),
    )


async def load_llm_notes(
    engine: AsyncEngine,
    matches: Sequence[LiveMatch],
    *,
    candidate_ids: set[int] | None = None,
    now: datetime | None = None,
) -> dict[int, LlmNote]:
    """Notes for the listed matches. Candidate ids default to the next-goal rule.

    (``LONE_WEAK``) is evaluated on the full next-goal board, not only on
    ``matches``, so a match page does not get a false lone-candidate gap.
    """
    if not matches:
        return {}
    clock = now or datetime.now(UTC)
    board = [match for match, _reasons in await list_next_goal_matches(engine)]
    by_id = {match.fixture_id: match for match in board}
    for match in matches:
        by_id.setdefault(match.fixture_id, match)
    scope = tuple(by_id.values())
    from predictor.postgres import make_session_factory

    factory = make_session_factory(engine)
    async with factory() as session:
        if candidate_ids is None:
            legs = await load_first_legs(engine, scope)
            candidate_ids = {
                match.fixture_id
                for match in scope
                if next_goal_reasons(match, legs.get(match.fixture_id))
            }
        notes = await _notes(session, scope, candidate_ids=candidate_ids, now=clock)
    wanted = {match.fixture_id for match in matches}
    return {fid: note for fid, note in notes.items() if fid in wanted}


async def load_sandbox_coupon_labels(
    engine: AsyncEngine, fixture_ids: Sequence[int] | None = None
) -> dict[int, str]:
    """Open sandbox coupons covering these fixtures → corner badge text."""
    from predictor.postgres import make_session_factory

    factory = make_session_factory(engine)
    async with factory() as session:
        return await open_sandbox_coupon_labels(session, fixture_ids)


async def _notes(
    session: AsyncSession,
    matches: Sequence[LiveMatch],
    *,
    candidate_ids: set[int],
    now: datetime,
) -> dict[int, LlmNote]:
    strategy_id, minute_limit, min_odd, paused = await _llm_strategy(session)
    fixture_ids = [match.fixture_id for match in matches]
    quotes = await latest_over_under(session, fixture_ids)
    opened: frozenset[int] = frozenset()
    rejected: frozenset[tuple[int, str, int, int, int]] = frozenset()
    decisions: dict[int, AdvisorDecision] = {}
    if strategy_id is not None:
        opened = await open_fixture_ids(session, strategy_id)
        rejected = await rejected_states(session, strategy_id)
        decisions = await _latest_decisions(session, strategy_id, fixture_ids)
    limits = PlayLimits(
        max_minute_regulation=minute_limit,
        max_minute_extra=110,
        max_quote_age=QUOTE_MAX_AGE,
    )
    notes: dict[int, LlmNote] = {}
    snapshots: dict[int, MatchSnapshot] = {}
    for match in matches:
        snap = _snapshot(match)
        snapshots[match.fixture_id] = snap
        rows = quotes.get(match.fixture_id, ())
        gaps = llm_prompt_gaps(
            snap,
            rows,
            is_candidate=match.fixture_id in candidate_ids,
            limits=limits,
            now=now,
            open_fixtures=opened,
            rejected=rejected,
        )
        period = period_for(match.status_short)
        shown_limit = (
            limits.max_minute_extra if period is Period.EXTRA_TIME else minute_limit
        )
        notes[match.fixture_id] = LlmNote(
            gaps=gaps,
            minute_limit=shown_limit,
            line=_line_label(match),
            decision=_decision_note(decisions.get(match.fixture_id), match),
            strategy_paused=paused,
        )
    _apply_lone_weak(notes, snapshots, quotes, min_combined_odd=min_odd)
    return notes


def _apply_lone_weak(
    notes: dict[int, LlmNote],
    snapshots: dict[int, MatchSnapshot],
    quotes: dict[int, tuple[OverUnderRow, ...]],
    *,
    min_combined_odd: Decimal,
) -> None:
    """Mark the sole prompt-ready match when G4 blocks a model call."""
    ready = [fid for fid, note in notes.items() if not note.gaps]
    if len(ready) != 1:
        return
    fixture_id = ready[0]
    snap = snapshots[fixture_id]
    period = period_for(snap.status_short)
    if period is None:
        return
    quote = quote_for_period(
        quotes.get(fixture_id, ()),
        period=period,
        goals_home=snap.goals_home,
        goals_away=snap.goals_away,
        extratime_home=snap.extratime_home,
        extratime_away=snap.extratime_away,
    )
    if quote is None or quote.odd is None:
        return
    n = _period_goals(snap, period)
    if n is None:
        return
    candidate = CycleCandidate(
        fixture_id=fixture_id,
        period=period.value,
        goals_home=snap.goals_home or 0,
        goals_away=snap.goals_away or 0,
        ordinal=n + 1,
        candidate_rule="",
        odd=quote.odd,
        n=n,
    )
    if lone_candidate_allowed((candidate,), min_combined_odd=min_combined_odd):
        return
    note = notes[fixture_id]
    notes[fixture_id] = replace(note, gaps=(*note.gaps, LlmGap.LONE_WEAK))


async def _llm_strategy(
    session: AsyncSession,
) -> tuple[int | None, int, Decimal, bool]:
    """Active sandbox first. A paused row still supplies its last decisions."""
    row = await _llm_row(session, statuses=_ACTIVE)
    if row is not None:
        return int(row[0]), int(row[1]), Decimal(str(row[2])), False
    paused = await _llm_row(session, statuses=frozenset({"paused"}))
    if paused is None:
        return None, _DEFAULT_MINUTE, Decimal("1.600"), False
    return int(paused[0]), int(paused[1]), Decimal(str(paused[2])), True


async def _llm_row(
    session: AsyncSession, *, statuses: frozenset[str]
) -> tuple[int, int, Decimal, str] | None:
    row = (
        await session.execute(
            select(
                AdvisorStrategy.id,
                AdvisorStrategyVersion.max_minute_regulation,
                AdvisorStrategyVersion.min_combined_odd,
                AdvisorStrategy.status,
            )
            .join(
                AdvisorStrategyVersion,
                AdvisorStrategyVersion.strategy_id == AdvisorStrategy.id,
            )
            .where(AdvisorStrategy.kind == "llm")
            .where(AdvisorStrategy.status.in_(statuses))
            .order_by(AdvisorStrategy.id, AdvisorStrategyVersion.version_no.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return int(row[0]), int(row[1]), Decimal(str(row[2])), str(row[3])


async def _latest_decisions(
    session: AsyncSession, strategy_id: int, fixture_ids: Sequence[int]
) -> dict[int, AdvisorDecision]:
    if not fixture_ids:
        return {}
    rows = (
        await session.scalars(
            select(AdvisorDecision)
            .where(AdvisorDecision.strategy_id == strategy_id)
            .where(AdvisorDecision.fixture_id.in_(fixture_ids))
            .order_by(AdvisorDecision.fixture_id, AdvisorDecision.id.desc())
            .distinct(AdvisorDecision.fixture_id)
        )
    ).all()
    return {int(row.fixture_id): row for row in rows}


def _decision_note(
    row: AdvisorDecision | None, match: LiveMatch
) -> LlmDecisionNote | None:
    if row is None:
        return None
    period = period_for(match.status_short)
    same = (
        period is not None
        and row.period == period.value
        and match.goals_home == row.goals_home
        and match.goals_away == row.goals_away
    )
    return LlmDecisionNote(
        decision=row.decision,
        rationale=row.rationale,
        goals_home=row.goals_home,
        goals_away=row.goals_away,
        same_state=same,
    )
