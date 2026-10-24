"""One advisor pass: baseline coupons now, one LLM call after the debounce."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from predictor.models.advisor import AdvisorStrategy, AdvisorStrategyVersion
from predictor.schemas.advisor import AdvisorSettings
from predictor.services.advisor.candidates import (
    MatchSnapshot,
    candidate_signature,
    playable_candidate,
)
from predictor.services.advisor.cycle import CycleCandidate, CycleRequest, run_cycle
from predictor.services.advisor.domain import QUOTE_MAX_AGE, PlayLimits
from predictor.services.advisor.langfuse import TraceSink
from predictor.services.advisor.llm.protocol import LlmProvider
from predictor.services.advisor.prompts.cycle import PROMPT_VERSION, render_cycle_prompt
from predictor.services.advisor.recommend import (
    RecommendationSetup,
    inactive_reason,
    league_allowed,
)
from predictor.services.advisor.recommend_ops import (
    open_recommendation_fixtures,
    recommendation_usage,
)
from predictor.services.advisor.sandbox import place_baseline
from predictor.services.advisor.sql import (
    OverUnderRow,
    last_signature,
    latest_over_under,
    open_fixture_ids,
    rejected_states,
)
from predictor.services.live_board.domain import (
    FirstLegScore,
    LiveMatch,
    select_next_goal_matches,
)
from predictor.services.live_board.sql import list_live_matches, load_first_legs
from predictor.services.match_detail import load_match_detail
from predictor.services.queue import untracked_league_ids

POLL_SECONDS = 15
BASELINE_NAME = "baseline"
LLM_NAME = "sandbox-llm"
_BOARD_RULES = ["favorite_losing", "tie_deficit"]
_ACTIVE = frozenset({"sandbox", "promoted"})


def board_snapshots(
    matches: Sequence[LiveMatch],
    first_legs: dict[int, FirstLegScore] | None = None,
) -> tuple[MatchSnapshot, ...]:
    """The one candidate list. Baseline and every LLM strategy share it."""
    return tuple(
        MatchSnapshot(
            fixture_id=match.fixture_id,
            status_short=match.status_short,
            elapsed=match.elapsed,
            extra=match.extra,
            goals_home=match.goals_home,
            goals_away=match.goals_away,
            extratime_home=match.et_home,
            extratime_away=match.et_away,
            rule=rules[0],
            league_id=match.league_id,
        )
        for match, rules in select_next_goal_matches(matches, first_legs)
        if rules
    )


@dataclass(frozen=True, slots=True)
class StrategyBook:
    strategy_id: int
    version_id: int
    kind: str
    status: str
    min_odd: Decimal
    max_legs: int
    max_minute_regulation: int
    max_minute_extra: int


@dataclass
class CandidateWindow:
    """In-memory debounce. The last fired set is reloaded from Postgres."""

    signature: tuple[tuple[int, str, int, int, int], ...] = ()
    since: datetime | None = None
    fired: bool = True

    def ready(
        self,
        signature: tuple[tuple[int, str, int, int, int], ...],
        now: datetime,
        wait: timedelta,
    ) -> bool:
        if signature != self.signature:
            self.signature = signature
            self.since = now
            self.fired = False
            return False
        if not signature or self.fired or self.since is None:
            return False
        return now - self.since >= wait

    def mark_fired(self) -> None:
        """The set was sent to the model. A budget skip must not call this."""
        self.fired = True

    def next_sleep(self, now: datetime, wait: timedelta, poll: float) -> float:
        """Sleep until this set is due, and never longer than the poll."""
        if self.fired or not self.signature or self.since is None:
            return poll
        remaining = (self.since + wait - now).total_seconds()
        if remaining <= 0:
            return poll
        return min(poll, remaining)


async def run_tick(
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    provider: LlmProvider,
    settings: AdvisorSettings,
    window: CandidateWindow,
    now: datetime,
    *,
    extra_windows: dict[int, CandidateWindow] | None = None,
    recommendation: RecommendationSetup | None = None,
    traces: TraceSink | None = None,
    redact_secrets: tuple[str, ...] = (),
) -> bool:
    """Place baseline coupons and, when the window is due, call the model once.

    Returns whether the model was called.
    """
    matches = await list_live_matches(engine)
    legs = await load_first_legs(engine, matches)
    selected = board_snapshots(matches, legs)
    async with factory() as session:
        async with session.begin():
            baseline = await ensure_strategy(session, BASELINE_NAME, "baseline")
            books = await _active_llm(session)
            quotes = await latest_over_under(
                session,
                [item.fixture_id for item in selected],
            )
            baseline_candidates = await _playable(
                session, baseline, selected, quotes, now, for_llm=False
            )
            prepared = []
            for book in books:
                candidates = await _playable(
                    session, book, selected, quotes, now, for_llm=True
                )
                opened = await open_fixture_ids(session, book.strategy_id)
                prepared.append((book, candidates, opened))
            reco_open = await open_recommendation_fixtures(session)
            ignored = frozenset(await untracked_league_ids(session))
            coupons_today, loss_today = (0, Decimal("0"))
            if recommendation is not None:
                coupons_today, loss_today = await recommendation_usage(session, now)
        if baseline.status in _ACTIVE and baseline_candidates:
            await place_baseline(
                session,
                strategy_id=baseline.strategy_id,
                strategy_version_id=baseline.version_id,
                candidates=baseline_candidates,
                clock=lambda: now,
            )
        called = False
        wait = timedelta(seconds=settings.advisor_debounce_seconds)
        gate = (
            None
            if recommendation is None
            else inactive_reason(
                recommendation,
                now,
                coupons_today=coupons_today,
                loss_today=loss_today,
            )
        )
        allowed = frozenset(
            item.fixture_id
            for item in selected
            if recommendation is not None
            and item.fixture_id not in reco_open
            and league_allowed(item.league_id, recommendation, untracked=ignored)
        )
        windows = extra_windows if extra_windows is not None else {}
        for index, (book, candidates, opened) in enumerate(prepared):
            own = (
                window
                if index == 0
                else windows.setdefault(book.strategy_id, CandidateWindow())
            )
            signature = candidate_signature(candidates)
            if book.status not in _ACTIVE or not own.ready(signature, now, wait):
                continue
            details = await _match_details(
                engine, [item.fixture_id for item in candidates]
            )
            prompt, body = render_cycle_prompt(
                candidates,
                min_combined_odd=book.min_odd,
                max_legs=book.max_legs,
                match_details=details,
            )
            recommend = book.status == "promoted" and gate is None
            outcome = await run_cycle(
                session,
                provider,
                settings,
                CycleRequest(
                    strategy_id=book.strategy_id,
                    strategy_version_id=book.version_id,
                    mode="recommendation" if recommend else "sandbox",
                    prompt_version=PROMPT_VERSION,
                    prompt=prompt,
                    input_json=body,
                    candidates=tuple(candidates),
                    open_fixture_ids=opened,
                    min_odd=book.min_odd,
                    max_legs=book.max_legs,
                    recommend=recommend,
                    recommendation_stake=(
                        None if recommendation is None else recommendation.stake
                    ),
                    recommendation_fixtures=allowed,
                ),
                clock=lambda: now,
                traces=traces,
                redact_secrets=redact_secrets,
            )
            if outcome.called:
                own.mark_fired()
                called = True
        return called


async def restore_window(session: AsyncSession, strategy_id: int) -> CandidateWindow:
    signature = await last_signature(session, strategy_id)
    return CandidateWindow(signature=signature, fired=True)


async def _active_llm(session: AsyncSession) -> list[StrategyBook]:
    rows = (
        await session.scalars(
            select(AdvisorStrategy)
            .where(
                AdvisorStrategy.kind == "llm",
                AdvisorStrategy.status.in_(_ACTIVE),
            )
            .order_by(AdvisorStrategy.id)
        )
    ).all()
    books: list[StrategyBook] = []
    for row in rows:
        version = await session.scalar(
            select(AdvisorStrategyVersion)
            .where(AdvisorStrategyVersion.strategy_id == row.id)
            .order_by(AdvisorStrategyVersion.version_no.desc())
            .limit(1)
        )
        if version is None:
            continue
        books.append(
            StrategyBook(
                strategy_id=row.id,
                version_id=version.id,
                kind=row.kind,
                status=row.status,
                min_odd=version.min_combined_odd,
                max_legs=version.max_legs,
                max_minute_regulation=version.max_minute_regulation,
                max_minute_extra=version.max_minute_extra,
            )
        )
    if books:
        return books
    return [await ensure_strategy(session, LLM_NAME, "llm")]


async def ensure_strategy(session: AsyncSession, name: str, kind: str) -> StrategyBook:
    """Use an active strategy of this kind, or create the named sandbox row."""
    row = await session.scalar(
        select(AdvisorStrategy)
        .where(
            AdvisorStrategy.kind == kind,
            AdvisorStrategy.status.in_(_ACTIVE),
        )
        .order_by(AdvisorStrategy.id)
        .limit(1)
    )
    if row is None:
        row = AdvisorStrategy(name=name, kind=kind, status="sandbox")
        session.add(row)
        await session.flush()
    version = await session.scalar(
        select(AdvisorStrategyVersion)
        .where(AdvisorStrategyVersion.strategy_id == row.id)
        .order_by(AdvisorStrategyVersion.version_no.desc())
        .limit(1)
    )
    if version is None:
        version = AdvisorStrategyVersion(
            strategy_id=row.id,
            version_no=1,
            prompt_version=PROMPT_VERSION,
            min_combined_odd=Decimal("1.600"),
            max_legs=3,
            max_minute_regulation=80,
            max_minute_extra=110,
            wait_minutes=10,
            candidate_rules=list(_BOARD_RULES),
        )
        session.add(version)
        await session.flush()
    return StrategyBook(
        strategy_id=row.id,
        version_id=version.id,
        kind=row.kind,
        status=row.status,
        min_odd=version.min_combined_odd,
        max_legs=version.max_legs,
        max_minute_regulation=version.max_minute_regulation,
        max_minute_extra=version.max_minute_extra,
    )


async def _match_details(
    engine: AsyncEngine, fixture_ids: Sequence[int]
) -> dict[int, dict[str, Any]]:
    unique = tuple(dict.fromkeys(fixture_ids))
    if not unique:
        return {}
    loaded = await asyncio.gather(
        *(load_match_detail(engine, fixture_id) for fixture_id in unique)
    )
    return {
        fixture_id: detail.as_llm_dict()
        for fixture_id, detail in zip(unique, loaded, strict=True)
        if detail is not None
    }


async def _playable(
    session: AsyncSession,
    book: StrategyBook,
    matches: Sequence[MatchSnapshot],
    quotes: dict[int, tuple[OverUnderRow, ...]],
    now: datetime,
    *,
    for_llm: bool,
) -> tuple[CycleCandidate, ...]:
    limits = PlayLimits(
        max_minute_regulation=book.max_minute_regulation,
        max_minute_extra=book.max_minute_extra,
        max_quote_age=QUOTE_MAX_AGE,
    )
    opened = await open_fixture_ids(session, book.strategy_id)
    rejected = await rejected_states(session, book.strategy_id)
    chosen: list[CycleCandidate] = []
    for match in matches:
        candidate = playable_candidate(
            match,
            quotes.get(match.fixture_id, ()),
            limits=limits,
            now=now,
            open_fixtures=opened,
            rejected=rejected,
            for_llm=for_llm,
        )
        if candidate is not None:
            chosen.append(candidate)
    return tuple(chosen)
