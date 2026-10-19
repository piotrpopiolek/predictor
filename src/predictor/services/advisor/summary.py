"""One daily proposal of a new sandbox strategy. It never promotes itself."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.models.advisor import (
    AdvisorCycle,
    AdvisorStrategy,
    AdvisorStrategyEvent,
    AdvisorStrategyVersion,
)
from predictor.services.advisor.llm.protocol import LlmCompletion

_ACTIVE = frozenset({"sandbox", "promoted"})
_MAX = 4


class StrategyProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_combined_odd: Decimal = Field(gt=1)
    max_legs: int = Field(ge=1, le=3)
    max_minute_regulation: int = Field(ge=1, le=120)
    max_minute_extra: int = Field(ge=91, le=130)
    wait_minutes: int = Field(ge=1, le=30)
    notes: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class SummaryJob:
    strategy_id: int
    version_id: int
    prompt: str


def parse_proposal(raw: str) -> StrategyProposal:
    return StrategyProposal.model_validate_json(raw)


async def due_summaries(session: AsyncSession, now: datetime) -> tuple[SummaryJob, ...]:
    """Strategies with no summary cycle yet today, after 06:00 UTC."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if now.hour < 6:
        return ()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    strategies = (
        await session.scalars(
            select(AdvisorStrategy).where(
                AdvisorStrategy.kind == "llm",
                AdvisorStrategy.status.in_(_ACTIVE),
            )
        )
    ).all()
    jobs: list[SummaryJob] = []
    for strategy in strategies:
        already = await session.scalar(
            select(AdvisorCycle.id)
            .join(
                AdvisorStrategyVersion,
                AdvisorStrategyVersion.id == AdvisorCycle.strategy_version_id,
            )
            .where(AdvisorStrategyVersion.strategy_id == strategy.id)
            .where(AdvisorCycle.prompt_version == "summary")
            .where(AdvisorCycle.started_at >= start)
            .limit(1)
        )
        if already is not None:
            continue
        version = await session.scalar(
            select(AdvisorStrategyVersion)
            .where(AdvisorStrategyVersion.strategy_id == strategy.id)
            .order_by(AdvisorStrategyVersion.version_no.desc())
            .limit(1)
        )
        if version is None:
            continue
        jobs.append(
            SummaryJob(strategy.id, version.id, _prompt(strategy.name, version))
        )
    return tuple(jobs)


async def store_summary(
    session: AsyncSession,
    job: SummaryJob,
    completion: LlmCompletion,
    *,
    cost: Decimal,
    now: datetime,
) -> bool:
    """Persist the call. A valid proposal becomes a new sandbox or proposed strategy."""
    try:
        proposal = parse_proposal(completion.text)
        status = "ok"
    except ValidationError:
        proposal = None
        status = "invalid_output"
    session.add(
        AdvisorCycle(
            strategy_version_id=job.version_id,
            mode="sandbox",
            provider=completion.provider,
            model=completion.model,
            model_version=completion.model_version,
            prompt_version="summary",
            prompt_text=job.prompt,
            input_json={"strategy_id": job.strategy_id},
            raw_output=completion.text,
            validation_status=status,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            thoughts_tokens=completion.thoughts_tokens,
            cost_pln=cost,
            finished_at=now,
        )
    )
    if proposal is None:
        return False
    version = await session.get(AdvisorStrategyVersion, job.version_id)
    if version is None:
        return False
    active = await session.scalar(
        select(func.count())
        .select_from(AdvisorStrategy)
        .where(AdvisorStrategy.kind == "llm")
        .where(AdvisorStrategy.status.in_(_ACTIVE))
    )
    new_status = "sandbox" if int(active or 0) < _MAX else "proposed"
    day = now.astimezone(UTC).date().isoformat()
    created = AdvisorStrategy(
        name=f"summary-{job.strategy_id}-{day}",
        kind="llm",
        status=new_status,
    )
    session.add(created)
    await session.flush()
    session.add(
        AdvisorStrategyVersion(
            strategy_id=created.id,
            version_no=1,
            prompt_version="summary",
            min_combined_odd=proposal.min_combined_odd,
            max_legs=proposal.max_legs,
            max_minute_regulation=proposal.max_minute_regulation,
            max_minute_extra=proposal.max_minute_extra,
            wait_minutes=proposal.wait_minutes,
            candidate_rules=list(version.candidate_rules),
            notes=proposal.notes,
        )
    )
    session.add(
        AdvisorStrategyEvent(
            strategy_id=created.id,
            previous_status="proposed",
            new_status=new_status,
            source="summary",
            note=proposal.notes,
        )
    )
    return True


def _prompt(name: str, version: AdvisorStrategyVersion) -> str:
    return (
        "Zaproponuj nową wersję strategii next goal jako JSON. "
        f"Strategia {name}, próg {version.min_combined_odd}, "
        f"nogi {version.max_legs}, minuta {version.max_minute_regulation}. "
        "Pola: min_combined_odd, max_legs, max_minute_regulation, "
        "max_minute_extra, wait_minutes, notes."
    )
