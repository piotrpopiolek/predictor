"""Advisor process loop. One instance, sandbox only, no API-Football."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.constants import ADVISOR_LOCK_APPLICATION_NAME, ADVISOR_LOCK_KEY
from predictor.logutil import configure_logging, log_json
from predictor.models.advisor import AdvisorCycle
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.advisor import AdvisorSettings, load_advisor_settings
from predictor.schemas.settings import SettingsError, load_settings
from predictor.services.advisor.budget import (
    budget_allows,
    call_cost,
    estimate_call_cost,
    utc_day_bounds,
)
from predictor.services.advisor.langfuse import sink_from_settings
from predictor.services.advisor.llm.gemini import GeminiProvider
from predictor.services.advisor.llm.protocol import ProviderError
from predictor.services.advisor.recommend import inactive_reason, setup_for
from predictor.services.advisor.recommend_ops import expire_due, withdraw_promoted
from predictor.services.advisor.settle import settle_finished
from predictor.services.advisor.summary import due_summaries, store_summary
from predictor.services.advisor.telegram import (
    deliver_pending,
    enqueue,
    post_telegram,
)
from predictor.services.advisor.tick import (
    LLM_NAME,
    POLL_SECONDS,
    CandidateWindow,
    ensure_strategy,
    restore_window,
    run_tick,
)
from predictor.services.lock import LockBusyError, WriterLock, lock_busy_message

logger = logging.getLogger(__name__)


def _install_stop_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            break


async def run_advisor(stop: asyncio.Event) -> None:
    worker_settings = load_settings()
    advisor_settings = load_advisor_settings()
    configure_logging()
    traces = sink_from_settings(advisor_settings)
    recommendation = setup_for(
        stake=advisor_settings.recommendation_stake,
        loss_limit=advisor_settings.recommendation_loss_limit,
        hours=advisor_settings.operator_hours,
        leagues=advisor_settings.recommendation_leagues,
        coupon_limit=advisor_settings.recommendation_coupon_limit,
        ttl_seconds=advisor_settings.recommendation_ttl_seconds,
        withdraw_yield=advisor_settings.withdraw_yield,
    )
    gate = inactive_reason(
        recommendation,
        datetime.now(UTC),
        coupons_today=0,
        loss_today=Decimal("0"),
    )
    log_json(
        logging.INFO,
        service="advisor",
        event="startup",
        recommendation="active" if gate is None else gate,
        langfuse="on" if traces is not None else "off",
    )
    secrets = (
        advisor_settings.llm_api_key.get_secret_value(),
        advisor_settings.langfuse_public_key.get_secret_value(),
        advisor_settings.langfuse_secret_key.get_secret_value(),
    )
    provider = GeminiProvider(
        api_key=advisor_settings.llm_api_key.get_secret_value(),
        model=advisor_settings.llm_model,
    )
    engine = make_async_engine(worker_settings)
    factory = make_session_factory(engine)
    lock = WriterLock(
        worker_settings,
        key=ADVISOR_LOCK_KEY,
        application_name=ADVISOR_LOCK_APPLICATION_NAME,
    )
    acquired = await lock.try_acquire()
    if not acquired:
        info = await lock.holder_info()
        await lock.close()
        log_json(logging.WARNING, service="advisor", event="lock_busy")
        raise LockBusyError(lock_busy_message(info, ADVISOR_LOCK_KEY))
    log_json(
        logging.INFO,
        service="advisor",
        event="lock_acquired",
        backend_pid=lock.backend_pid,
    )
    try:
        async with factory() as session, session.begin():
            book = await ensure_strategy(session, LLM_NAME, "llm")
            window = await restore_window(session, book.strategy_id)
        windows: dict[int, CandidateWindow] = {}
        while not stop.is_set():
            now = datetime.now(UTC)
            async with factory() as session, session.begin():
                await settle_finished(session, now)
                await expire_due(session, now, recommendation.ttl_seconds)
                note = await withdraw_promoted(session, recommendation.withdraw_yield)
                if note:
                    enqueue(session, "withdraw", note)
            await run_tick(
                engine,
                factory,
                provider,
                advisor_settings,
                window,
                now,
                extra_windows=windows,
                recommendation=recommendation,
                traces=traces,
                redact_secrets=secrets,
            )
            token = advisor_settings.telegram_bot_token.get_secret_value()
            async with factory() as session, session.begin():
                await deliver_pending(
                    session,
                    token=token,
                    chat_id=advisor_settings.telegram_chat_id,
                    post=post_telegram,
                )
            await _summarize(
                factory,
                provider,
                advisor_settings,
                now,
                token,
                advisor_settings.telegram_chat_id,
            )
            delay = window.next_sleep(
                datetime.now(UTC),
                timedelta(seconds=advisor_settings.advisor_debounce_seconds),
                POLL_SECONDS,
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue
    finally:
        await lock.release()
        await lock.close()
        await engine.dispose()
        log_json(logging.INFO, service="advisor", event="lock_released")


def _configure_event_loop() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def main() -> None:
    _configure_event_loop()
    stop = asyncio.Event()
    try:
        asyncio.run(_main(stop))
    except SettingsError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    except LockBusyError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


async def _summarize(
    factory: async_sessionmaker[AsyncSession],
    provider: GeminiProvider,
    settings: AdvisorSettings,
    now: datetime,
    token: str,
    chat: str,
) -> None:
    async with factory() as session, session.begin():
        start, _end = utc_day_bounds(now)
        spent = await session.scalar(
            select(func.coalesce(func.sum(AdvisorCycle.cost_pln), 0)).where(
                AdvisorCycle.started_at >= start
            )
        )
        jobs = await due_summaries(session, now)
    for job in jobs:
        estimate = estimate_call_cost(
            job.prompt,
            price_input=settings.llm_price_input_pln_per_mtok,
            price_output=settings.llm_price_output_pln_per_mtok,
        )
        if not budget_allows(
            Decimal(0 if spent is None else spent),
            estimate,
            mode="recommendation",
            limit=settings.advisor_daily_budget_pln,
        ):
            return
        try:
            completion = await provider.decide(
                job.prompt, timeout=settings.llm_timeout_seconds
            )
        except ProviderError:
            continue
        cost = call_cost(
            completion.input_tokens,
            completion.output_tokens,
            completion.thoughts_tokens,
            price_input=settings.llm_price_input_pln_per_mtok,
            price_output=settings.llm_price_output_pln_per_mtok,
        )
        try:
            async with factory() as session, session.begin():
                await store_summary(session, job, completion, cost=cost, now=now)
                if token and chat:
                    enqueue(
                        session,
                        "summary",
                        f"Nowa propozycja strategii z podsumowania {job.strategy_id}.",
                    )
                    await deliver_pending(
                        session, token=token, chat_id=chat, post=post_telegram
                    )
        except IntegrityError:
            log_json(
                logging.WARNING,
                service="advisor",
                event="summary_name_conflict",
                strategy_id=job.strategy_id,
            )
            continue


async def _main(stop: asyncio.Event) -> None:
    _install_stop_signals(stop)
    await run_advisor(stop)
