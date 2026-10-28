"""Pure next-goal rules for the advisor. No database, clock, or HTTP.

A bet is Over N+0.5 inside one settlement period. A goal in the other
period does not settle it. Callers pass the quote for that period's own
Over/Under market; this module does not borrow the other period's line.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum

from predictor.constants import (
    FINISHED_FIXTURE_STATUSES,
    IRREGULAR_FIXTURE_STATUSES,
    PAYOUT_KEEP,
)

_ODD_QUANTUM = Decimal("0.001")
_MONEY_QUANTUM = Decimal("0.01")
_ONE = Decimal(1)
# Single-leg floor to enter LLM analysis. Coupon profitability stays on
# strategy min_combined_odd (typically 1.600) across 1–3 legs.
MIN_ANALYSIS_ODD = Decimal("1.300")
# Freshness of the Over N+0.5 snapshot for every strategy (baseline and LLM).
QUOTE_MAX_AGE = timedelta(seconds=300)
# LLM prompt: 2H or extra time, Over N+0.5 open, odd >= MIN_ANALYSIS_ODD,
# snapshot within QUOTE_MAX_AGE. Baseline also uses 1H/HT.
LLM_STATUS = "2H"
LLM_STATUSES = frozenset({"2H", "ET", "BT"})

REGULATION_OVER_UNDER = "over/under line"
# Live dictionary name (lowercased). Same Over value labels as regulation.
EXTRA_TIME_OVER_UNDER = "over/under extra time"

_REGULATION = frozenset({"1H", "HT", "2H"})
_EXTRA_TIME = frozenset({"ET", "BT"})
_PENALTIES = frozenset({"P"})


class Period(StrEnum):
    REGULATION = "regulation"
    EXTRA_TIME = "extra_time"


class Skip(StrEnum):
    NO_LINE = "no_line"
    SUSPENDED = "suspended"
    STALE_ODD = "stale_odd"
    MINUTE_CUTOFF = "minute_cutoff"
    PENALTIES = "penalties"
    NOT_IN_PLAY = "not_in_play"
    OPEN_COUPON = "open_coupon"
    REJECTED_STATE = "rejected_state"


class Settlement(StrEnum):
    PENDING = "pending"
    WON = "won"
    LOST = "lost"
    VOID = "void"
    MANUAL = "manual"


class LiveProgress(StrEnum):
    """Live preview of a leg or coupon. Not a ledger settlement."""

    HIT = "hit"
    OPEN = "open"
    LOST = "lost"
    VOID = "void"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StateKey:
    fixture_id: int
    period: Period
    goals_home: int
    goals_away: int
    ordinal: int


@dataclass(frozen=True, slots=True)
class LineQuote:
    odd: Decimal | None
    tradable: bool
    captured_at: datetime | None


@dataclass(frozen=True, slots=True)
class CandidateView:
    fixture_id: int
    status_short: str
    elapsed: int | None
    extra: int | None
    goals_home: int | None
    goals_away: int | None
    extratime_home: int | None
    extratime_away: int | None
    quote: LineQuote | None
    market_known: bool
    in_open_coupon: bool
    state_rejected: bool


@dataclass(frozen=True, slots=True)
class PlayLimits:
    max_minute_regulation: int
    max_minute_extra: int
    max_quote_age: timedelta


@dataclass(frozen=True, slots=True)
class Playability:
    code: Skip | None
    period: Period | None
    n: int | None
    state: StateKey | None

    @property
    def playable(self) -> bool:
        return self.code is None


@dataclass(frozen=True, slots=True)
class FinalScore:
    status_short: str
    fulltime_home: int | None
    fulltime_away: int | None
    extratime_home: int | None
    extratime_away: int | None
    final_refreshed: bool
    awaiting_manual: bool


@dataclass(frozen=True, slots=True)
class LiveScore:
    """Fixture scoreboard for a live coupon preview. No settlement flags."""

    status_short: str
    goals_home: int | None
    goals_away: int | None
    fulltime_home: int | None
    fulltime_away: int | None
    extratime_home: int | None
    extratime_away: int | None


# Regulation period is over once the match leaves 1H/HT/2H (including ET).
_REGULATION_ENDED = frozenset({"ET", "BT", "P", "FT", "AET", "PEN"})
# Extra-time period ends at penalties or a finished status.
_EXTRA_TIME_ENDED = frozenset({"P", "FT", "AET", "PEN"})
# Interrupt mid-match: keep reading the live board for the open period.
_IN_PLAY_PAUSE = frozenset({"SUSP", "INT", "LIVE"})


def period_for(status_short: str) -> Period | None:
    if status_short in _REGULATION:
        return Period.REGULATION
    if status_short in _EXTRA_TIME:
        return Period.EXTRA_TIME
    return None


def over_half_line(n: int) -> Decimal:
    """The Over price line for N goals already scored: N+0.5."""
    return (Decimal(n) + Decimal("0.5")).quantize(Decimal("0.1"))


def over_line_matches(label: str, handicap: str, n: int) -> bool:
    """True when this live row is Over N+0.5, not another total."""
    target = over_half_line(n)
    folded = " ".join(label.strip().casefold().split())
    handicap_line = _line_number(handicap)
    if folded == "over" and handicap_line == target:
        return True
    if folded.startswith("over "):
        named = _line_number(folded.removeprefix("over "))
        return named == target and handicap_line in (None, target)
    return False


def over_under_market(period: Period) -> str | None:
    if period is Period.REGULATION:
        return REGULATION_OVER_UNDER
    return EXTRA_TIME_OVER_UNDER


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


def assess(candidate: CandidateView, limits: PlayLimits, now: datetime) -> Playability:
    if candidate.status_short in _PENALTIES:
        return _blocked(Skip.PENALTIES)
    period = period_for(candidate.status_short)
    if period is None:
        return _blocked(Skip.NOT_IN_PLAY)
    score = _scoreboard(candidate)
    if score is None:
        return _blocked(Skip.NOT_IN_PLAY, period)
    n = _period_goals(period, score, candidate)
    state = StateKey(
        fixture_id=candidate.fixture_id,
        period=period,
        goals_home=score[0],
        goals_away=score[1],
        ordinal=n + 1,
    )
    minute = match_minute(candidate.status_short, candidate.elapsed, candidate.extra)
    limit = (
        limits.max_minute_extra
        if period is Period.EXTRA_TIME
        else limits.max_minute_regulation
    )
    if minute is None or minute > limit:
        return _blocked(Skip.MINUTE_CUTOFF, period, n, state)
    blocked = _quote_block(candidate, limits.max_quote_age, now)
    if blocked is None and candidate.in_open_coupon:
        blocked = Skip.OPEN_COUPON
    if blocked is None and candidate.state_rejected:
        blocked = Skip.REJECTED_STATE
    if blocked is not None:
        return _blocked(blocked, period, n, state)
    return Playability(code=None, period=period, n=n, state=state)


def wait_continues(started: Period, current: Period) -> bool:
    """A wait does not cross from regulation into extra time."""
    return started == current


def combined_odd(odds: Sequence[Decimal]) -> Decimal:
    if not odds:
        raise ValueError("coupon has no legs")
    total = _ONE
    for odd in odds:
        if odd <= _ONE:
            raise ValueError("odd must be above 1")
        total *= odd
    return total.quantize(_ODD_QUANTUM, rounding=ROUND_HALF_UP)


def coupon_within_limits(
    odds: Sequence[Decimal], *, min_odd: Decimal, max_legs: int
) -> bool:
    if not odds or len(odds) > max_legs:
        return False
    try:
        total = combined_odd(odds)
    except ValueError:
        return False
    return total >= min_odd


def settle_leg(period: Period, n: int, score: FinalScore) -> Settlement:
    if score.status_short in IRREGULAR_FIXTURE_STATUSES:
        return Settlement.VOID
    if score.status_short not in FINISHED_FIXTURE_STATUSES:
        if score.awaiting_manual:
            return Settlement.MANUAL
        return Settlement.PENDING
    if not score.final_refreshed:
        return Settlement.PENDING
    total = _final_goals(period, score)
    if total is None:
        return Settlement.MANUAL
    if total > n:
        return Settlement.WON
    return Settlement.LOST


def settle_coupon(outcomes: Sequence[Settlement]) -> Settlement:
    """One lost leg loses the coupon even while another leg is still open."""
    if not outcomes:
        raise ValueError("coupon has no legs")
    if any(item is Settlement.LOST for item in outcomes):
        return Settlement.LOST
    if any(item is Settlement.MANUAL for item in outcomes):
        return Settlement.MANUAL
    if any(item is Settlement.PENDING for item in outcomes):
        return Settlement.PENDING
    active = [item for item in outcomes if item is not Settlement.VOID]
    if not active:
        return Settlement.VOID
    return Settlement.WON


def period_score_now(period: Period, score: LiveScore) -> tuple[int, int] | None:
    """Home/away goals that settle the period right now. None when incomplete."""
    status = score.status_short
    if period is Period.REGULATION:
        if status in _REGULATION_ENDED:
            return _pair(score.fulltime_home, score.fulltime_away)
        if status in _REGULATION or status in _IN_PLAY_PAUSE:
            if (
                status in _IN_PLAY_PAUSE
                and score.fulltime_home is not None
                and score.fulltime_away is not None
            ):
                return score.fulltime_home, score.fulltime_away
            return _pair(score.goals_home, score.goals_away)
        return None
    et = _pair(score.extratime_home, score.extratime_away)
    if et is not None:
        return et
    if status in _EXTRA_TIME or status in _IN_PLAY_PAUSE:
        live = _pair(score.goals_home, score.goals_away)
        fulltime = _pair(score.fulltime_home, score.fulltime_away)
        if live is None or fulltime is None:
            return None
        return live[0] - fulltime[0], live[1] - fulltime[1]
    return None


def period_goals_now(period: Period, score: LiveScore) -> int | None:
    """Goals that settle the period right now. None when the board is incomplete."""
    board = period_score_now(period, score)
    if board is None:
        return None
    return board[0] + board[1]


def live_leg_progress(period: Period, n: int, score: LiveScore) -> LiveProgress:
    """Preview whether the Over N+0.5 condition already holds. Not a settlement."""
    if score.status_short in IRREGULAR_FIXTURE_STATUSES:
        return LiveProgress.VOID
    goals = period_goals_now(period, score)
    if goals is None:
        # Period over without a usable board: treat as failed, not unknown.
        if _period_finished(period, score.status_short):
            return LiveProgress.LOST
        return LiveProgress.UNKNOWN
    if goals > n:
        return LiveProgress.HIT
    if _period_finished(period, score.status_short):
        return LiveProgress.LOST
    return LiveProgress.OPEN


def live_coupon_progress(outcomes: Sequence[LiveProgress]) -> LiveProgress:
    """Same precedence as settle_coupon; hit/open/unknown stand in for won/pending."""
    if not outcomes:
        raise ValueError("coupon has no legs")
    if any(item is LiveProgress.LOST for item in outcomes):
        return LiveProgress.LOST
    if any(item is LiveProgress.UNKNOWN for item in outcomes):
        return LiveProgress.UNKNOWN
    if any(item is LiveProgress.OPEN for item in outcomes):
        return LiveProgress.OPEN
    active = [item for item in outcomes if item is not LiveProgress.VOID]
    if not active:
        return LiveProgress.VOID
    return LiveProgress.HIT


def active_combined_odd(
    legs: Sequence[tuple[Decimal, Settlement]],
) -> Decimal | None:
    """Product of odds that are not void. None while the coupon is open."""
    if any(item is Settlement.PENDING or item is Settlement.MANUAL for _, item in legs):
        return None
    active = [odd for odd, item in legs if item is not Settlement.VOID]
    if not active:
        return None
    return combined_odd(active)


def settled_pnl(
    stake: Decimal,
    odd: Decimal,
    outcome: Settlement,
    keep: Decimal = PAYOUT_KEEP,
) -> Decimal:
    if outcome is Settlement.WON:
        return (stake * odd * keep - stake).quantize(
            _MONEY_QUANTUM, rounding=ROUND_HALF_UP
        )
    if outcome is Settlement.LOST:
        return (-stake).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    if outcome is Settlement.VOID:
        return Decimal("0.00")
    raise ValueError("coupon is not settled")


def _line_number(text: str) -> Decimal | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return Decimal(stripped).quantize(Decimal("0.1"))
    except InvalidOperation:
        return None


def _scoreboard(candidate: CandidateView) -> tuple[int, int] | None:
    if candidate.goals_home is None or candidate.goals_away is None:
        return None
    return candidate.goals_home, candidate.goals_away


def _period_goals(
    period: Period, score: tuple[int, int], candidate: CandidateView
) -> int:
    if period is Period.REGULATION:
        return score[0] + score[1]
    return (candidate.extratime_home or 0) + (candidate.extratime_away or 0)


def _quote_block(
    candidate: CandidateView, max_age: timedelta, now: datetime
) -> Skip | None:
    if not candidate.market_known:
        return Skip.NO_LINE
    quote = candidate.quote
    if quote is None or quote.odd is None or quote.odd <= _ONE:
        return Skip.NO_LINE
    if not quote.tradable:
        return Skip.SUSPENDED
    captured = quote.captured_at
    if captured is None or _stale(captured, now, max_age):
        return Skip.STALE_ODD
    return None


def _stale(captured: datetime, now: datetime, max_age: timedelta) -> bool:
    if captured.tzinfo is None or now.tzinfo is None:
        return True
    age = now - captured
    return age < timedelta(0) or age > max_age


def _final_goals(period: Period, score: FinalScore) -> int | None:
    if period is Period.REGULATION:
        if score.fulltime_home is None or score.fulltime_away is None:
            return None
        return score.fulltime_home + score.fulltime_away
    if score.extratime_home is None or score.extratime_away is None:
        return None
    return score.extratime_home + score.extratime_away


def _period_finished(period: Period, status_short: str) -> bool:
    if period is Period.REGULATION:
        return status_short in _REGULATION_ENDED
    return status_short in _EXTRA_TIME_ENDED


def _pair(home: int | None, away: int | None) -> tuple[int, int] | None:
    if home is None or away is None:
        return None
    return home, away


def _blocked(
    code: Skip,
    period: Period | None = None,
    n: int | None = None,
    state: StateKey | None = None,
) -> Playability:
    return Playability(code=code, period=period, n=n, state=state)
