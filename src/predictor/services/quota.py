"""Quota budget: keep enough requests for live matches until the daily reset.

Temporary reset is 22:00 Europe/Warsaw, not 00:00 UTC. Revert ``quota_reset_after``
when the vendor window returns to midnight UTC.

History (priorities 5–9) and live odds spend only the surplus above the
reserve. Detail cadence follows matches actually in play plus the kickoff
forecast, and slows only when that forecast does not fit above the safety
buffer. An empty board polls scores every 5 minutes instead of every minute.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from predictor.client.quota import QuotaSnapshot
from predictor.constants import (
    IDLE_SCORE_POLL_SECONDS,
    LIVE_CONTEXT_CREDIT_TICKS,
    LIVE_CONTEXT_FINAL_CALLS,
    LIVE_CONTEXT_MAX_CALLS_PER_TICK,
    LIVE_CONTEXT_ONESHOT_CALLS,
    LIVE_DETAIL_REFRESH_SECONDS,
    LIVE_REQUESTS_PER_TICK,
    MATCH_LIVE_HOURS,
    QUOTA_SNAPSHOT_ENDPOINT,
)
from predictor.models.etl import EtlTask

# Temporary vendor window. 22:00 local follows CET/CEST.
QUOTA_RESET_TZ = ZoneInfo("Europe/Warsaw")
QUOTA_RESET_LOCAL_TIME = time(22, 0)


@dataclass(frozen=True, slots=True)
class KickoffLoad:
    """Matches that share one kickoff instant."""

    kickoff: datetime
    count: int


@dataclass(frozen=True, slots=True)
class DayLoadForecast:
    """Not-yet-live kickoffs still expected to consume a live window today."""

    kickoffs: tuple[KickoffLoad, ...] = ()

    @property
    def matches(self) -> int:
        return sum(max(0, item.count) for item in self.kickoffs)


@dataclass(frozen=True, slots=True)
class _Window:
    start: datetime
    end: datetime
    count: int


@dataclass(frozen=True, slots=True)
class _Segment:
    start: datetime
    end: datetime
    concurrency: int


@dataclass(frozen=True, slots=True)
class LiveBudgetPlan:
    """Requests to hold until reset, and how fast live work may run."""

    score_need: int
    detail_need: int
    odds_need: int
    detail_refresh_seconds: int
    oneshot_calls: int
    score_poll_seconds: float
    poll_odds: bool
    relaxed_refresh_seconds: int = LIVE_DETAIL_REFRESH_SECONDS
    overloaded: bool = False
    safety_buffer: int = 0
    oneshot_reserve: int = 0
    max_context_calls: int = 0
    context_calls_per_tick: float = 0.0
    allowed_per_minute: float = 0.0

    @property
    def full_reserve(self) -> int:
        return self.score_need + self.detail_need + self.odds_need

    @property
    def reserved_calls(self) -> int:
        """Calls this plan intends to spend above the safety buffer."""
        return self.full_reserve + self.oneshot_reserve


@dataclass
class LiveSpendGate:
    """Mutable view of the latest plan, read by the live-context handler."""

    detail_refresh_seconds: int = LIVE_DETAIL_REFRESH_SECONDS
    relaxed_refresh_seconds: int = LIVE_DETAIL_REFRESH_SECONDS
    oneshot_calls: int = LIVE_CONTEXT_ONESHOT_CALLS
    max_context_calls: int = LIVE_CONTEXT_MAX_CALLS_PER_TICK
    spent_calls: int = 0


@dataclass
class ContextCredit:
    """Fractional P3 allowance carried across ticks. Lost on process restart."""

    balance: float = 0.0


def safety_buffer_requests(limit_day: int, buffer_percent: float) -> int:
    """Calls that stay untouched until the vendor reset."""
    if limit_day <= 0 or buffer_percent <= 0:
        return 0
    return math.ceil(limit_day * (buffer_percent / 100.0))


def plan_live_budget(
    *,
    live_matches: int,
    remaining: int,
    now: datetime,
    target_seconds: int = 60,
    matches_left_today: int | None = None,
    forecast: DayLoadForecast | None = None,
    finishing_matches: int = 0,
    limit_day: int = 0,
    buffer_percent: float = 0.0,
) -> LiveBudgetPlan:
    """Reserve score, detail, and odds from the calls left above the buffer.

    ``live_matches`` is the board right now. ``forecast`` is kickoffs that
    have not started. A bare ``matches_left_today`` count is treated as that
    many matches kicking off now, for callers that do not have timestamps.
    Detail interval uses those windows, never the raw count of every fixture
    dated today. The score poll stays on the idle cadence until something is
    actually in play.
    """
    utc_now = _as_utc(now)
    midnight = quota_reset_after(utc_now)
    seconds_left = max(1.0, (midnight - utc_now).total_seconds())
    buffer = safety_buffer_requests(limit_day, buffer_percent)
    usable = max(0, remaining - buffer)
    in_play = live_matches > 0
    finishing = max(0, finishing_matches)
    load = _resolve_forecast(
        forecast=forecast,
        matches_left_today=matches_left_today,
        now=utc_now,
    )
    windows = _windows(
        now=utc_now,
        midnight=midnight,
        live_matches=max(0, live_matches),
        forecast=load,
    )
    segments = _segments(utc_now, midnight, windows)
    match_seconds = _match_seconds(windows, utc_now, midnight)
    live_seconds = _busy_seconds(segments)
    score_polls = _integrated_polls(
        segments,
        busy_seconds=float(target_seconds),
        idle_seconds=float(IDLE_SCORE_POLL_SECONDS),
    )
    odds_polls = _integrated_polls(
        segments,
        busy_seconds=float(target_seconds),
        idle_seconds=0.0,
    )
    ideal_score = math.ceil(score_polls) if score_polls > 0 else 0
    ideal_odds = math.ceil(odds_polls) if odds_polls > 0 else 0
    live_ticks = live_seconds / float(target_seconds) if target_seconds > 0 else 0.0
    fast = LIVE_DETAIL_REFRESH_SECONDS

    if usable <= 0:
        return _stopped_plan(
            buffer=buffer,
            seconds_left=seconds_left,
            in_play=in_play,
        )

    if ideal_score > usable:
        scale = ideal_score / float(usable)
        sleep = float(target_seconds) if in_play else float(IDLE_SCORE_POLL_SECONDS)
        sleep *= scale
        return _assemble_plan(
            score_need=usable,
            detail_need=0,
            odds_need=0,
            interval=max(fast, math.ceil(seconds_left)),
            oneshot_calls=0,
            oneshot_reserve=0,
            sleep=sleep,
            poll_odds=False,
            overloaded=True,
            buffer=buffer,
            in_play=False,
            live_matches=0,
            finishing=0,
            tick=sleep,
        )

    score_need = ideal_score
    sleep = float(target_seconds) if in_play else float(IDLE_SCORE_POLL_SECONDS)
    detail_at_fast = _detail_calls(match_seconds, finishing, fast)
    choice = _fit_quality(
        usable=usable,
        score_need=score_need,
        detail_at_fast=detail_at_fast,
        ideal_odds=ideal_odds,
        live_ticks=live_ticks,
        match_seconds=match_seconds,
        finishing=finishing,
        seconds_left=seconds_left,
    )
    return _assemble_plan(
        score_need=score_need,
        detail_need=choice.detail_need,
        odds_need=choice.odds_need,
        interval=choice.interval,
        relaxed_interval=choice.relaxed_interval,
        oneshot_calls=choice.oneshot_calls if in_play else 0,
        oneshot_reserve=choice.oneshot_reserve,
        sleep=sleep,
        poll_odds=in_play and choice.odds_need > 0,
        overloaded=choice.overloaded,
        buffer=buffer,
        in_play=in_play,
        live_matches=max(0, live_matches),
        finishing=finishing,
        tick=sleep,
    )


@dataclass(frozen=True, slots=True)
class _Quality:
    interval: int
    relaxed_interval: int
    detail_need: int
    odds_need: int
    oneshot_calls: int
    oneshot_reserve: int
    overloaded: bool


# Share of a match spent at halftime or in the last minutes of the first half.
_URGENT_MATCH_SHARE = 0.25


def quota_allows(priority: int, snapshot: QuotaSnapshot, plan: LiveBudgetPlan) -> bool:
    """History and odds run only above the live reserve. Scores always run."""
    remaining = snapshot.remaining
    if remaining <= 0 or remaining <= plan.safety_buffer:
        return False
    if priority <= 2:
        return True
    protected = plan.safety_buffer + plan.score_need
    if remaining <= protected:
        return False
    if priority == 3:
        return plan.context_calls_per_tick > 0
    if plan.overloaded:
        return False
    if priority == 4:
        return plan.poll_odds and remaining > protected + plan.detail_need
    return remaining > plan.safety_buffer + plan.full_reserve


def draw_context_credit(credit: ContextCredit, rate: float) -> int:
    """Add up to ``rate`` calls and return the whole calls available now."""
    if rate <= 0:
        return 0
    cap = rate * LIVE_CONTEXT_CREDIT_TICKS
    credit.balance = min(cap, credit.balance + rate)
    allowed = math.floor(credit.balance)
    credit.balance -= allowed
    return int(allowed)


def refund_context_credit(credit: ContextCredit, unused: int, rate: float) -> None:
    """Put unused whole calls back, still capped at a few ticks of ``rate``."""
    if unused <= 0 or rate <= 0:
        return
    cap = rate * LIVE_CONTEXT_CREDIT_TICKS
    credit.balance = min(cap, credit.balance + float(unused))


def context_allowance(credit: ContextCredit, rate: float, *, room: int) -> int:
    """Draw a P3 cap that cannot spend past ``room`` calls."""
    drawn = draw_context_credit(credit, rate)
    allowed = drawn if room >= drawn else max(0, room)
    refund_context_credit(credit, drawn - allowed, rate)
    return allowed


def _stopped_plan(*, buffer: int, seconds_left: float, in_play: bool) -> LiveBudgetPlan:
    sleep = seconds_left
    return _assemble_plan(
        score_need=0,
        detail_need=0,
        odds_need=0,
        interval=LIVE_DETAIL_REFRESH_SECONDS,
        oneshot_calls=0,
        oneshot_reserve=0,
        sleep=sleep,
        poll_odds=False,
        overloaded=True,
        buffer=buffer,
        in_play=in_play,
        live_matches=0,
        finishing=0,
        tick=sleep,
    )


def _assemble_plan(
    *,
    score_need: int,
    detail_need: int,
    odds_need: int,
    interval: int,
    relaxed_interval: int | None = None,
    oneshot_calls: int,
    oneshot_reserve: int,
    sleep: float,
    poll_odds: bool,
    overloaded: bool,
    buffer: int,
    in_play: bool,
    live_matches: int,
    finishing: int,
    tick: float,
) -> LiveBudgetPlan:
    relaxed = interval if relaxed_interval is None else relaxed_interval
    if relaxed < interval:
        relaxed = interval
    rate = 0.0
    if in_play and live_matches > 0 and tick > 0:
        if interval > 0:
            rate += live_matches * _URGENT_MATCH_SHARE * tick / float(interval)
        if relaxed > 0:
            rate += live_matches * (1.0 - _URGENT_MATCH_SHARE) * tick / float(relaxed)
    if finishing > 0 and interval > 0 and tick > 0 and detail_need > 0:
        paced = finishing * tick / float(interval)
        rate += min(float(LIVE_CONTEXT_FINAL_CALLS), paced)
    if in_play and oneshot_calls > 0:
        rate += float(oneshot_calls)
    capped = min(float(LIVE_CONTEXT_MAX_CALLS_PER_TICK), rate)
    allowed = (1.0 + capped) * 60.0 / tick if tick > 0 else 0.0
    return LiveBudgetPlan(
        score_need=score_need,
        detail_need=detail_need,
        odds_need=odds_need,
        detail_refresh_seconds=max(1, int(interval)),
        relaxed_refresh_seconds=max(1, int(relaxed)),
        oneshot_calls=max(0, oneshot_calls),
        score_poll_seconds=sleep,
        poll_odds=poll_odds,
        overloaded=overloaded,
        safety_buffer=buffer,
        oneshot_reserve=max(0, oneshot_reserve),
        max_context_calls=min(LIVE_CONTEXT_MAX_CALLS_PER_TICK, math.floor(capped)),
        context_calls_per_tick=capped,
        allowed_per_minute=allowed,
    )


def _fit_quality(
    *,
    usable: int,
    score_need: int,
    detail_at_fast: int,
    ideal_odds: int,
    live_ticks: float,
    match_seconds: float,
    finishing: int,
    seconds_left: float,
) -> _Quality:
    fast = LIVE_DETAIL_REFRESH_SECONDS

    def reserve(cap: int) -> int:
        if cap <= 0 or live_ticks <= 0:
            return 0
        return math.ceil(cap * live_ticks)

    def fits(detail: int, odds: int, oneshots: int) -> bool:
        return score_need + detail + odds + oneshots <= usable

    full_oneshots = reserve(LIVE_CONTEXT_ONESHOT_CALLS)
    if fits(detail_at_fast, ideal_odds, full_oneshots):
        return _Quality(
            interval=fast,
            relaxed_interval=fast,
            detail_need=detail_at_fast,
            odds_need=ideal_odds,
            oneshot_calls=LIVE_CONTEXT_ONESHOT_CALLS,
            oneshot_reserve=full_oneshots,
            overloaded=False,
        )
    reduced_oneshots = reserve(4)
    if fits(detail_at_fast, ideal_odds, reduced_oneshots):
        return _Quality(
            interval=fast,
            relaxed_interval=fast,
            detail_need=detail_at_fast,
            odds_need=ideal_odds,
            oneshot_calls=4,
            oneshot_reserve=reduced_oneshots,
            overloaded=False,
        )
    if fits(detail_at_fast, ideal_odds, 0):
        return _Quality(
            interval=fast,
            relaxed_interval=fast,
            detail_need=detail_at_fast,
            odds_need=ideal_odds,
            oneshot_calls=0,
            oneshot_reserve=0,
            overloaded=False,
        )
    if fits(detail_at_fast, 0, 0):
        return _Quality(
            interval=fast,
            relaxed_interval=fast,
            detail_need=detail_at_fast,
            odds_need=0,
            oneshot_calls=0,
            oneshot_reserve=0,
            overloaded=ideal_odds > 0,
        )
    detail_budget = max(0, usable - score_need)
    urgent_interval, relaxed_interval, detail_need = _tiered_intervals(
        match_seconds=match_seconds,
        finishing=finishing,
        call_budget=detail_budget,
        seconds_left=seconds_left,
    )
    return _Quality(
        interval=urgent_interval,
        relaxed_interval=relaxed_interval,
        detail_need=detail_need,
        odds_need=0,
        oneshot_calls=0,
        oneshot_reserve=0,
        overloaded=True,
    )


def _tiered_intervals(
    *,
    match_seconds: float,
    finishing: int,
    call_budget: int,
    seconds_left: float,
) -> tuple[int, int, int]:
    """Keep halftime-sized detail faster and stretch the rest of the match."""
    fast = LIVE_DETAIL_REFRESH_SECONDS
    ceiling = max(fast, math.ceil(seconds_left))
    if call_budget <= 0:
        return ceiling, ceiling, 0
    finish = min(max(0, finishing), call_budget)
    body = call_budget - finish
    if match_seconds <= 0 or body <= 0:
        return fast, fast, finish
    urgent_seconds = match_seconds * _URGENT_MATCH_SHARE
    relaxed_seconds = match_seconds - urgent_seconds
    urgent_fast = _calls_only(urgent_seconds, fast)
    if urgent_fast <= body:
        relaxed_body = body - urgent_fast
        relaxed_interval = _interval_only(relaxed_seconds, relaxed_body, seconds_left)
        relaxed_calls = min(
            relaxed_body, _calls_only(relaxed_seconds, relaxed_interval)
        )
        return fast, max(fast, relaxed_interval), urgent_fast + relaxed_calls + finish
    urgent_body = max(1, body // 2)
    relaxed_body = max(0, body - urgent_body)
    urgent_interval = _interval_only(urgent_seconds, urgent_body, seconds_left)
    relaxed_interval = _interval_only(
        relaxed_seconds, max(1, relaxed_body), seconds_left
    )
    if relaxed_interval < urgent_interval:
        relaxed_interval = urgent_interval
    urgent_calls = min(urgent_body, _calls_only(urgent_seconds, urgent_interval))
    relaxed_calls = min(relaxed_body, _calls_only(relaxed_seconds, relaxed_interval))
    return urgent_interval, relaxed_interval, urgent_calls + relaxed_calls + finish


def _calls_only(match_seconds: float, interval: int) -> int:
    if match_seconds <= 0 or interval <= 0:
        return 0
    return math.ceil(match_seconds / float(interval))


def _interval_only(match_seconds: float, call_budget: int, seconds_left: float) -> int:
    ceiling = max(LIVE_DETAIL_REFRESH_SECONDS, math.ceil(seconds_left))
    if match_seconds <= 0 or call_budget <= 0:
        return ceiling
    interval = max(LIVE_DETAIL_REFRESH_SECONDS, math.ceil(match_seconds / call_budget))
    while _calls_only(match_seconds, interval) > call_budget and interval < ceiling:
        interval += 1
    return min(interval, ceiling)


def _detail_calls(match_seconds: float, finishing: int, interval: int) -> int:
    calls = 0
    if match_seconds > 0 and interval > 0:
        calls = math.ceil(match_seconds / float(interval))
    return calls + max(0, finishing)


def _resolve_forecast(
    *,
    forecast: DayLoadForecast | None,
    matches_left_today: int | None,
    now: datetime,
) -> DayLoadForecast:
    if forecast is not None:
        return forecast
    if matches_left_today is None or matches_left_today <= 0:
        return DayLoadForecast()
    return DayLoadForecast(
        kickoffs=(KickoffLoad(kickoff=now, count=matches_left_today),)
    )


def _windows(
    *,
    now: datetime,
    midnight: datetime,
    live_matches: int,
    forecast: DayLoadForecast,
) -> list[_Window]:
    span = timedelta(hours=MATCH_LIVE_HOURS)
    windows: list[_Window] = []
    if live_matches > 0:
        end = min(midnight, now + span)
        if end > now:
            windows.append(_Window(now, end, live_matches))
    for item in forecast.kickoffs:
        count = max(0, item.count)
        if count <= 0:
            continue
        kickoff = _as_utc(item.kickoff)
        start = max(kickoff, now)
        end = min(kickoff + span, midnight)
        if end <= start:
            continue
        windows.append(_Window(start, end, count))
    return windows


def _segments(
    now: datetime, midnight: datetime, windows: list[_Window]
) -> list[_Segment]:
    points = {now, midnight}
    for window in windows:
        if now < window.start < midnight:
            points.add(window.start)
        if now < window.end < midnight:
            points.add(window.end)
    ordered = sorted(points)
    segments: list[_Segment] = []
    for left, right in zip(ordered, ordered[1:], strict=False):
        if right <= left:
            continue
        mid = left + (right - left) / 2
        concurrency = sum(
            window.count for window in windows if window.start <= mid < window.end
        )
        segments.append(_Segment(left, right, concurrency))
    return segments


def _match_seconds(windows: list[_Window], now: datetime, midnight: datetime) -> float:
    total = 0.0
    for window in windows:
        start = max(window.start, now)
        end = min(window.end, midnight)
        if end > start:
            total += window.count * (end - start).total_seconds()
    return total


def _busy_seconds(segments: list[_Segment]) -> float:
    return sum(
        (segment.end - segment.start).total_seconds()
        for segment in segments
        if segment.concurrency > 0
    )


def _integrated_polls(
    segments: list[_Segment], *, busy_seconds: float, idle_seconds: float
) -> float:
    total = 0.0
    for segment in segments:
        duration = (segment.end - segment.start).total_seconds()
        if segment.concurrency > 0:
            if busy_seconds > 0:
                total += duration / busy_seconds
        elif idle_seconds > 0:
            total += duration / idle_seconds
    return total


def _as_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def quota_reset_after(now: datetime) -> datetime:
    """Next temporary reset: 22:00 Europe/Warsaw, as a UTC instant."""
    utc_now = _as_utc(now)
    local = utc_now.astimezone(QUOTA_RESET_TZ)
    candidate = datetime.combine(
        local.date(), QUOTA_RESET_LOCAL_TIME, tzinfo=QUOTA_RESET_TZ
    )
    if local >= candidate:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def seconds_until_utc_midnight(now: datetime) -> float:
    """Seconds until the temporary 22:00 Europe/Warsaw quota reset."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    utc_now = now.astimezone(UTC)
    return max(1.0, (quota_reset_after(utc_now) - utc_now).total_seconds())


def live_poll_interval_seconds(
    snapshot: QuotaSnapshot,
    target_seconds: int,
    now: datetime,
    *,
    requests_per_tick: int = LIVE_REQUESTS_PER_TICK,
) -> float:
    """Return the sleep between live ticks. Stretch past the target when quota
    cannot sustain it until the quota reset (FR-019)."""
    target = float(target_seconds)
    if snapshot.remaining <= 0:
        return seconds_until_utc_midnight(now)
    cost = max(1, requests_per_tick)
    seconds_left = seconds_until_utc_midnight(now)
    projected = (seconds_left / target) * cost
    if projected <= snapshot.remaining:
        return target
    max_ticks = snapshot.remaining // cost
    if max_ticks <= 0:
        return seconds_until_utc_midnight(now)
    return max(target, seconds_left / max_ticks)


def live_poll_interval_gauge(
    remaining: int,
    used: int,
    limit_day: int,
    target_seconds: int,
    now: datetime,
) -> float:
    """Interval Prometheus should compare snapshot age against (FR-019)."""
    if remaining < 0:
        return float(target_seconds)
    return live_poll_interval_seconds(
        QuotaSnapshot(
            current=used,
            limit_day=max(limit_day, 1),
            remaining=remaining,
            source="api",
        ),
        target_seconds,
        now,
    )


def quota_gauges_from_params(params: dict[str, Any] | None) -> tuple[int, int]:
    """Return (remaining, used). remaining is -1 when unknown."""
    if not params:
        return -1, 0
    try:
        remaining = int(params["remaining"])
        used = int(params.get("current", 0))
    except (KeyError, TypeError, ValueError):
        return -1, 0
    return remaining, used


async def persist_quota_snapshot(
    session: AsyncSession,
    snapshot: QuotaSnapshot,
    *,
    score_poll_seconds: float | None = None,
    detail_refresh_seconds: int | None = None,
    context_call_cap: float | None = None,
    overloaded: bool | None = None,
    allowed_per_minute: float | None = None,
) -> None:
    if snapshot.source != "api":
        return
    params: dict[str, Any] = {
        "current": snapshot.current,
        "limit_day": snapshot.limit_day,
        "remaining": snapshot.remaining,
        "source": snapshot.source,
        "fetched_at": (
            snapshot.fetched_at.isoformat() if snapshot.fetched_at is not None else None
        ),
    }
    if score_poll_seconds is not None:
        params["score_poll_seconds"] = score_poll_seconds
    if detail_refresh_seconds is not None:
        params["detail_refresh_seconds"] = detail_refresh_seconds
    if context_call_cap is not None:
        params["context_call_cap"] = context_call_cap
    if overloaded is not None:
        params["overloaded"] = bool(overloaded)
    if allowed_per_minute is not None:
        params["allowed_per_minute"] = allowed_per_minute
    task = await session.scalar(
        select(EtlTask)
        .where(EtlTask.endpoint == QUOTA_SNAPSHOT_ENDPOINT)
        .order_by(EtlTask.id)
        .limit(1)
    )
    now = datetime.now(UTC)
    if task is None:
        session.add(
            EtlTask(
                endpoint=QUOTA_SNAPSHOT_ENDPOINT,
                params=params,
                status="complete",
                completed_at=now,
            )
        )
        return
    task.params = params
    flag_modified(task, "params")
    task.status = "complete"
    task.updated_at = now
