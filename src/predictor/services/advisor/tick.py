"""One advisor pass: baseline coupons now, one LLM call after the debounce."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

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
from predictor.services.advisor.domain import PlayLimits
from predictor.services.advisor.langfuse import TraceSink
from predictor.services.advisor.llm.protocol import LlmProvider
from predictor.services.advisor.prompts.cycle import PROMPT_VERSION, render_cycle_prompt
from predictor.services.advisor.sandbox import place_baseline
from predictor.services.advisor.sql import (
    OverUnderRow,
    last_signature,
    latest_over_under,
    open_fixture_ids,
    rejected_states,
)
from predictor.services.live_board.domain import select_next_goal_matches
from predictor.services.live_board.sql import list_live_matches, load_first_legs

POLL_SECONDS = 15
QUOTE_MAX_AGE = timedelta(seconds=90)
BASELINE_NAME = "baseline"
LLM_NAME = "sandbox-llm"
_ACTIVE = frozenset({"sandbox", "promoted"})


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
        if now - self.since < wait:
            return False
        self.fired = True
        return True


async def run_tick(
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    provider: LlmProvider,
    settings: AdvisorSettings,
    window: CandidateWindow,
    now: datetime,
    *,
    traces: TraceSink | None = None,
    redact_secrets: tuple[str, ...] = (),
) -> bool:
    """Place baseline coupons and, when the window is due, call the model once.

    Returns whether the model was called.
    """
    matches = await list_live_matches(engine)
    legs = await load_first_legs(engine, matches)
    selected = [
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
        )
        for match, rules in select_next_goal_matches(matches, legs)
        if rules
    ]
    async with factory() as session:
        async with session.begin():
            baseline = await ensure_strategy(session, BASELINE_NAME, "baseline")
            llm = await ensure_strategy(session, LLM_NAME, "llm")
            quotes = await latest_over_under(
                session, [item.fixture_id for item in selected]
            )
            baseline_candidates = await _playable(
                session, baseline, selected, quotes, now
            )
            llm_candidates = await _playable(session, llm, selected, quotes, now)
            llm_open = await open_fixture_ids(session, llm.strategy_id)
        if baseline.status in _ACTIVE and baseline_candidates:
            await place_baseline(
                session,
                strategy_id=baseline.strategy_id,
                strategy_version_id=baseline.version_id,
                candidates=baseline_candidates,
                clock=lambda: now,
            )
        signature = candidate_signature(llm_candidates)
        if llm.status not in _ACTIVE or not window.ready(
            signature, now, timedelta(seconds=settings.advisor_debounce_seconds)
        ):
            return False
        prompt, body = render_cycle_prompt(llm_candidates)
        await run_cycle(
            session,
            provider,
            settings,
            CycleRequest(
                strategy_id=llm.strategy_id,
                strategy_version_id=llm.version_id,
                mode="sandbox",
                prompt_version=PROMPT_VERSION,
                prompt=prompt,
                input_json=body,
                candidates=tuple(llm_candidates),
                open_fixture_ids=llm_open,
                min_odd=llm.min_odd,
                max_legs=llm.max_legs,
            ),
            clock=lambda: now,
            traces=traces,
            redact_secrets=redact_secrets,
        )
    return True


async def restore_window(session: AsyncSession, strategy_id: int) -> CandidateWindow:
    signature = await last_signature(session, strategy_id)
    return CandidateWindow(signature=signature, fired=True)


async def ensure_strategy(session: AsyncSession, name: str, kind: str) -> StrategyBook:
    row = await session.scalar(
        select(AdvisorStrategy)
        .where(AdvisorStrategy.kind == kind)
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
            candidate_rules=["favorite_losing", "tie_deficit"],
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


async def _playable(
    session: AsyncSession,
    book: StrategyBook,
    matches: Sequence[MatchSnapshot],
    quotes: dict[int, tuple[OverUnderRow, ...]],
    now: datetime,
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
        )
        if candidate is not None:
            chosen.append(candidate)
    return tuple(chosen)
