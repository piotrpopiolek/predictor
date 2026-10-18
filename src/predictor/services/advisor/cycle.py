"""Run one model call and append the cycle.

A sandbox cycle whose status is ``ok`` writes its virtual coupons in that
same transaction. Any other status leaves no coupon. Recommendation
coupons are not created here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from predictor.constants import ADVISOR_CYCLE_LOCK_KEY
from predictor.models.advisor import AdvisorCycle, AdvisorDecision
from predictor.schemas.advisor import AdvisorSettings, CandidateDecision
from predictor.services.advisor.budget import (
    budget_allows,
    call_cost,
    estimate_call_cost,
    estimate_tokens,
    utc_day_bounds,
)
from predictor.services.advisor.llm.protocol import LlmProvider, ProviderError
from predictor.services.advisor.llm.validate import PricedCandidate, review_model_output
from predictor.services.advisor.sandbox import place_sandbox_coupons, reply_slips
from predictor.services.lock import hold_session_lock


@dataclass(frozen=True, slots=True)
class CycleCandidate:
    fixture_id: int
    period: str
    goals_home: int
    goals_away: int
    ordinal: int
    candidate_rule: str
    odd: Decimal
    n: int


@dataclass(frozen=True, slots=True)
class CycleRequest:
    strategy_id: int
    strategy_version_id: int
    mode: str
    prompt_version: str
    prompt: str
    input_json: dict[str, Any]
    candidates: tuple[CycleCandidate, ...]
    open_fixture_ids: frozenset[int]
    min_odd: Decimal
    max_legs: int


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    called: bool
    status: str
    cycle_id: int | None
    detail: str
    skipped_fixture_ids: tuple[int, ...] = ()
    coupon_ids: tuple[int, ...] = ()


def _clock() -> datetime:
    return datetime.now(UTC)


async def spent_today(session: AsyncSession, now: datetime) -> Decimal:
    start, end = utc_day_bounds(now)
    total = await session.scalar(
        select(func.coalesce(func.sum(AdvisorCycle.cost_pln), 0)).where(
            AdvisorCycle.started_at >= start,
            AdvisorCycle.started_at < end,
        )
    )
    if total is None:
        return Decimal(0)
    return Decimal(total)


async def run_cycle(
    session: AsyncSession,
    provider: LlmProvider,
    settings: AdvisorSettings,
    request: CycleRequest,
    *,
    clock: Callable[[], datetime] = _clock,
) -> CycleOutcome:
    """Append one cycle and commit it.

    The model call sits between two short transactions: the budget read and
    the insert. A session advisory lock is held across that gap, so a second
    cycle waits until this cost is committed before it reads the daily sum.
    A crash after the call still leaves the paid cost in the sum.

    A parsed rejection is stored only when the reply is valid. An invalid
    reply stays in ``raw_output`` so the match can be asked again. Virtual
    coupons are inserted in the same transaction as those decisions, and
    only when the reply is valid and the mode is sandbox.
    """
    _require_idle(session)
    async with hold_session_lock(_engine(session), ADVISOR_CYCLE_LOCK_KEY):
        return await _run_locked(session, provider, settings, request, clock=clock)


async def _run_locked(
    session: AsyncSession,
    provider: LlmProvider,
    settings: AdvisorSettings,
    request: CycleRequest,
    *,
    clock: Callable[[], datetime],
) -> CycleOutcome:
    now = clock()
    estimated = estimate_call_cost(
        request.prompt,
        price_input=settings.llm_price_input_pln_per_mtok,
        price_output=settings.llm_price_output_pln_per_mtok,
    )
    async with session.begin():
        spent = await spent_today(session, now)
    if not budget_allows(
        spent,
        estimated,
        mode=request.mode,
        limit=settings.advisor_daily_budget_pln,
    ):
        return CycleOutcome(False, "budget", None, "budget")
    started = perf_counter()
    try:
        _require_idle(session)
        completion = await provider.decide(
            request.prompt, timeout=float(settings.llm_timeout_seconds)
        )
    except ProviderError as exc:
        # The SDK error has no usage. Bill the pre-call estimate so the day
        # still counts a call Google may already have metered.
        input_tokens, output_tokens, thoughts_tokens = estimate_tokens(request.prompt)
        cycle_id, _skipped, _coupons = await _commit_cycle(
            session,
            _cycle(
                request,
                provider_name=provider.provider_name,
                model=provider.model,
                model_version=provider.model,
                status="provider_error",
                raw_output=None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                thoughts_tokens=thoughts_tokens,
                cost=estimated,
                started_at=now,
                finished_at=clock(),
                latency_ms=_latency(started),
            ),
        )
        return CycleOutcome(True, "provider_error", cycle_id, exc.code)
    verdict = review_model_output(
        completion.text,
        tuple(
            PricedCandidate(item.fixture_id, item.odd) for item in request.candidates
        ),
        set(request.open_fixture_ids),
        min_odd=request.min_odd,
        max_legs=request.max_legs,
    )
    cost = call_cost(
        completion.input_tokens,
        completion.output_tokens,
        completion.thoughts_tokens,
        price_input=settings.llm_price_input_pln_per_mtok,
        price_output=settings.llm_price_output_pln_per_mtok,
    )
    cycle = _cycle(
        request,
        provider_name=completion.provider,
        model=completion.model,
        model_version=completion.model_version,
        status=verdict.status,
        raw_output=completion.text,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        thoughts_tokens=completion.thoughts_tokens,
        cost=cost,
        started_at=now,
        finished_at=clock(),
        latency_ms=_latency(started),
    )
    parsed = verdict.decision if verdict.status == "ok" else None
    decisions = parsed.candidates if parsed is not None else ()
    coupon_fixtures = (
        tuple(coupon.fixture_ids for coupon in verdict.coupons)
        if parsed is not None
        else ()
    )
    cycle_id, skipped, coupon_ids = await _commit_cycle(
        session, cycle, request, decisions, coupon_fixtures
    )
    return CycleOutcome(
        True,
        verdict.status,
        cycle_id,
        verdict.detail,
        skipped_fixture_ids=skipped,
        coupon_ids=coupon_ids,
    )


def _cycle(
    request: CycleRequest,
    *,
    provider_name: str,
    model: str,
    model_version: str,
    status: str,
    raw_output: str | None,
    input_tokens: int,
    output_tokens: int,
    thoughts_tokens: int,
    cost: Decimal,
    started_at: datetime,
    finished_at: datetime,
    latency_ms: int,
) -> AdvisorCycle:
    return AdvisorCycle(
        strategy_version_id=request.strategy_version_id,
        mode=request.mode,
        started_at=started_at,
        finished_at=finished_at,
        provider=provider_name,
        model=model,
        model_version=model_version,
        prompt_version=request.prompt_version,
        prompt_text=request.prompt,
        input_json=request.input_json,
        raw_output=raw_output,
        validation_status=status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thoughts_tokens=thoughts_tokens,
        cost_pln=cost,
        latency_ms=latency_ms,
    )


def _engine(session: AsyncSession) -> AsyncEngine:
    bind = session.bind
    if not isinstance(bind, AsyncEngine):
        raise RuntimeError("run_cycle needs a bound async engine")
    return bind


def _require_idle(session: AsyncSession) -> None:
    if session.in_transaction():
        raise RuntimeError("run_cycle must be called without an open transaction")


async def _commit_cycle(
    session: AsyncSession,
    cycle: AdvisorCycle,
    request: CycleRequest | None = None,
    decisions: Sequence[CandidateDecision] = (),
    coupon_fixtures: Sequence[Sequence[int]] = (),
) -> tuple[int, tuple[int, ...], tuple[int, ...]]:
    skipped: list[int] = []
    coupon_ids: tuple[int, ...] = ()
    async with session.begin():
        session.add(cycle)
        await session.flush()
        if request is not None:
            stored: set[int] = set()
            for decision in decisions:
                if await _insert_decision(session, cycle.id, request, decision):
                    stored.add(decision.fixture_id)
                else:
                    skipped.append(decision.fixture_id)
            coupon_ids = await _place_if_sandbox(
                session, cycle, request, stored, coupon_fixtures
            )
        return cycle.id, tuple(skipped), coupon_ids


async def _place_if_sandbox(
    session: AsyncSession,
    cycle: AdvisorCycle,
    request: CycleRequest,
    stored: set[int],
    coupon_fixtures: Sequence[Sequence[int]],
) -> tuple[int, ...]:
    """Coupons share the decision transaction. Only a valid sandbox reply."""
    if request.mode != "sandbox" or cycle.validation_status != "ok":
        return ()
    if cycle.finished_at is None:
        return ()
    slips = reply_slips(coupon_fixtures, request.candidates, stored)
    if not slips:
        return ()
    placement = await place_sandbox_coupons(
        session,
        strategy_id=request.strategy_id,
        strategy_version_id=request.strategy_version_id,
        slips=slips,
        cycle_id=cycle.id,
        placed_at=cycle.finished_at,
        kind="llm",
    )
    return placement.coupon_ids


async def _insert_decision(
    session: AsyncSession,
    cycle_id: int,
    request: CycleRequest,
    decision: CandidateDecision,
) -> bool:
    """False when this reject or wait_timeout is already stored for the state."""
    context = _context(request, decision.fixture_id)
    try:
        async with session.begin_nested():
            session.add(_decision_row(cycle_id, request.strategy_id, context, decision))
            await session.flush()
    except IntegrityError as exc:
        if not _is_rejected_state_conflict(exc):
            raise
        return False
    return True


def _context(request: CycleRequest, fixture_id: int) -> CycleCandidate:
    for item in request.candidates:
        if item.fixture_id == fixture_id:
            return item
    raise KeyError(fixture_id)


def _is_rejected_state_conflict(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    return (
        getattr(diag, "constraint_name", None) == "uq_advisor_decisions_rejected_state"
    )


def _decision_row(
    cycle_id: int,
    strategy_id: int,
    context: CycleCandidate,
    decision: CandidateDecision,
) -> AdvisorDecision:
    return AdvisorDecision(
        cycle_id=cycle_id,
        strategy_id=strategy_id,
        fixture_id=context.fixture_id,
        period=context.period,
        goals_home=context.goals_home,
        goals_away=context.goals_away,
        ordinal=context.ordinal,
        candidate_rule=context.candidate_rule,
        decision=decision.decision,
        reasons=[reason.value for reason in decision.reasons],
        rationale=decision.rationale,
        estimated_probability=decision.estimated_probability,
        confidence=decision.confidence,
        odd_at_eval=context.odd,
        n=context.n,
    )


def _latency(started: float) -> int:
    return int((perf_counter() - started) * 1000)
