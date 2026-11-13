"""Langfuse export: redacted OTLP trace, and a failure that still keeps the decision."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select

from predictor.models.advisor import (
    AdvisorCycle,
    AdvisorDecision,
    AdvisorLangfuseOutbox,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.models.catalog import League
from predictor.models.fixtures import Fixture, Team
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.advisor import AdvisorSettings
from predictor.schemas.settings import Settings
from predictor.services.advisor.cycle import CycleCandidate, CycleRequest, run_cycle
from predictor.services.advisor.langfuse import (
    EU_HOST,
    MAX_EXPORT_ATTEMPTS,
    LangfuseSink,
    TraceDraft,
    TraceExportError,
    build_trace,
    deliver_trace,
    redact,
    retry_pending,
    sink_from_settings,
)
from predictor.services.advisor.llm.protocol import LlmCompletion

NOW = datetime(2026, 10, 18, 19, 0, tzinfo=UTC)
SECRET = "llm-key-do-not-leak"
LEAK = (
    f"use {SECRET} AIzaSyDUMMYKEY0123456789abcd "
    "postgresql+psycopg://predictor_app:s3cret@db.internal:5432/predictor_prod "
    "postgres://user:pass@db.internal/predictor "
    "Bearer eyJhbGciOi.part+tailsecret"
)


class _Provider:
    provider_name = "google"
    model = "gemini-2.5-flash"

    def __init__(self, text: str) -> None:
        self.text = text

    async def decide(
        self, prompt: str, *, timeout: float, response_schema: object
    ) -> LlmCompletion:
        return LlmCompletion(
            text=self.text,
            provider=self.provider_name,
            model=self.model,
            model_version="gemini-2.5-flash-001",
            input_tokens=12,
            output_tokens=4,
            thoughts_tokens=1,
        )


class _Sink:
    def __init__(self, error: TraceExportError | None = None) -> None:
        self.error = error
        self.bodies: list[dict[str, object]] = []

    async def send(self, body: dict[str, object]) -> None:
        self.bodies.append(body)
        if self.error is not None:
            raise self.error


def _settings() -> AdvisorSettings:
    return AdvisorSettings(
        llm_api_key="secret",
        llm_price_input_pln_per_mtok=Decimal("0.50"),
        llm_price_output_pln_per_mtok=Decimal("1.25"),
    )


_PLAIN = TraceDraft(
    prompt_version="p1",
    prompt="score",
    model_input={"note": "plain"},
    output="{}",
    provider="google",
    model="gemini-2.5-flash",
    model_version="gemini-2.5-flash-001",
    mode="sandbox",
    status="ok",
    input_tokens=12,
    output_tokens=4,
    thoughts_tokens=1,
    cost_pln=Decimal("0.0100"),
    latency_ms=8,
    started_at=NOW,
    finished_at=NOW,
)


def test_redact_strips_keys_dsns_and_bearer_tokens() -> None:
    cleaned = redact(
        {"note": LEAK, "nested": [LEAK]},
        (SECRET,),
    )
    rendered = json.dumps(cleaned)
    assert SECRET not in rendered
    assert "AIzaSy" not in rendered
    assert "postgres://" not in rendered
    assert "postgresql+psycopg" not in rendered
    assert "db.internal" not in rendered
    assert "predictor_prod" not in rendered
    assert "s3cret" not in rendered
    assert "tailsecret" not in rendered
    assert "[redacted]" in rendered


def test_provider_error_is_an_error_span() -> None:
    export = build_trace(
        replace(_PLAIN, status="provider_error", prompt=LEAK),
        (SECRET,),
    )
    rendered = json.dumps(export.body)
    assert SECRET not in rendered
    assert "db.internal" not in rendered
    assert "tailsecret" not in rendered
    span = export.body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == export.trace_id
    assert span["status"]["code"] == 2
    assert span["status"]["message"] == "provider_error"
    assert len(export.trace_id) == 32


def test_empty_langfuse_keys_skip_the_export() -> None:
    settings = _settings()
    assert settings.langfuse_host == EU_HOST
    assert sink_from_settings(settings) is None
    with pytest.raises(ValidationError):
        AdvisorSettings(
            llm_api_key="secret",
            llm_price_input_pln_per_mtok=Decimal("1"),
            llm_price_output_pln_per_mtok=Decimal("1"),
            langfuse_host="http://cloud.langfuse.com",
        )
    with pytest.raises(ValidationError):
        AdvisorSettings(
            llm_api_key="secret",
            llm_price_input_pln_per_mtok=Decimal("1"),
            llm_price_output_pln_per_mtok=Decimal("1"),
            langfuse_host="https://cloud.langfuse.com/api/public/otel",
        )
    trimmed = AdvisorSettings(
        llm_api_key="secret",
        llm_price_input_pln_per_mtok=Decimal("1"),
        llm_price_output_pln_per_mtok=Decimal("1"),
        langfuse_host="https://cloud.langfuse.com/",
    )
    assert trimmed.langfuse_host == EU_HOST


@pytest.mark.asyncio
async def test_sink_posts_otlp_to_the_eu_endpoint() -> None:
    seen: dict[str, str] = {}

    def ok(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["version"] = request.headers["x-langfuse-ingestion-version"]
        raw = request.headers["authorization"].split(" ", 1)[1]
        seen["auth"] = base64.b64decode(raw).decode()
        return httpx.Response(200)

    sink = LangfuseSink(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        transport=httpx.MockTransport(ok),
    )
    await sink.send({"resourceSpans": []})
    assert seen["url"] == f"{EU_HOST}/api/public/otel/v1/traces"
    assert seen["version"] == "4"
    assert seen["auth"] == "pk-lf-test:sk-lf-test"

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    failing = LangfuseSink(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        transport=httpx.MockTransport(down),
    )
    with pytest.raises(TraceExportError, match="http_503"):
        await failing.send({})


@pytest.mark.asyncio
async def test_a_cycle_stores_the_trace_and_keeps_the_ledger_intact() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    version_id = 0
    strategy_id = 0
    sink = _Sink()
    try:
        async with factory() as session:
            async with session.begin():
                version_id, strategy_id = await _seed(session)
            outcome = await run_cycle(
                session,
                _Provider(_reply(880_504)),
                _settings(),
                _request(strategy_id, version_id, 880_504),
                clock=lambda: NOW,
                traces=sink,
                redact_secrets=(SECRET,),
            )
            assert outcome.status == "ok"
            assert len(sink.bodies) == 1
            rendered = json.dumps(sink.bodies[0])
            assert SECRET not in rendered
            assert "db.internal" not in rendered
            assert "tailsecret" not in rendered
            assert "postgres://" not in rendered
            async with session.begin():
                cycle = await session.get(AdvisorCycle, outcome.cycle_id)
                row = await session.scalar(
                    select(AdvisorLangfuseOutbox).where(
                        AdvisorLangfuseOutbox.cycle_id == outcome.cycle_id
                    )
                )
            assert cycle is not None
            assert row is not None
            assert cycle.langfuse_trace_id == row.trace_id
            assert len(cycle.langfuse_trace_id or "") == 32
            assert SECRET in cycle.prompt_text
            assert row.status == "sent"
            assert row.attempts == 1
            assert row.trace_id in rendered
            assert "p1" in rendered
            assert "gemini-2.5-flash" in rendered
    finally:
        async with factory() as session, session.begin():
            await _wipe(session, strategy_id, version_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_export_failure_keeps_the_decision_and_then_stops_retrying() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    version_id = 0
    strategy_id = 0
    sink = _Sink(TraceExportError("timeout"))
    try:
        async with factory() as session:
            async with session.begin():
                version_id, strategy_id = await _seed(
                    session,
                    league=880_511,
                    home=880_512,
                    away=880_513,
                    fixture=880_514,
                    name="advisor-langfuse-retry",
                )
            outcome = await run_cycle(
                session,
                _Provider(_reply(880_514)),
                _settings(),
                _request(strategy_id, version_id, 880_514),
                clock=lambda: NOW,
                traces=sink,
            )
            assert outcome.status == "ok"
            async with session.begin():
                decisions = await session.scalar(
                    select(func.count())
                    .select_from(AdvisorDecision)
                    .where(AdvisorDecision.cycle_id == outcome.cycle_id)
                )
                row = await session.scalar(
                    select(AdvisorLangfuseOutbox).where(
                        AdvisorLangfuseOutbox.cycle_id == outcome.cycle_id
                    )
                )
            assert decisions == 1
            assert row is not None
            assert row.status == "pending"
            assert row.attempts == 1
            assert row.last_error == "timeout"
            for _ in range(MAX_EXPORT_ATTEMPTS):
                await retry_pending(session, sink)
            async with session.begin():
                done = await session.scalar(
                    select(AdvisorLangfuseOutbox).where(
                        AdvisorLangfuseOutbox.cycle_id == outcome.cycle_id
                    )
                )
            assert done is not None
            assert done.status == "abandoned"
            assert done.attempts == MAX_EXPORT_ATTEMPTS
    finally:
        async with factory() as session, session.begin():
            await _wipe(
                session,
                strategy_id,
                version_id,
                league=880_511,
                home=880_512,
                away=880_513,
                fixture=880_514,
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_second_sender_does_not_claim_the_row_in_flight() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    version_id = 0
    strategy_id = 0
    gate = _Gate()
    try:
        async with factory() as session:
            async with session.begin():
                version_id, strategy_id = await _seed(
                    session,
                    league=880_521,
                    home=880_522,
                    away=880_523,
                    fixture=880_524,
                    name="advisor-langfuse-race",
                )
            outcome = await run_cycle(
                session,
                _Provider(_reply(880_524)),
                _settings(),
                _request(strategy_id, version_id, 880_524),
                clock=lambda: NOW,
                traces=_Sink(TraceExportError("timeout")),
            )
            assert outcome.cycle_id is not None
            cycle_id = outcome.cycle_id
        async with factory() as left, factory() as right:
            lead = asyncio.create_task(deliver_trace(left, gate, cycle_id))
            await gate.entered.wait()
            follow = asyncio.create_task(deliver_trace(right, gate, cycle_id))
            await asyncio.wait_for(follow, timeout=2)
            assert gate.calls == 1
            gate.release.set()
            await lead
        async with factory() as session, session.begin():
            row = await session.scalar(
                select(AdvisorLangfuseOutbox).where(
                    AdvisorLangfuseOutbox.cycle_id == cycle_id
                )
            )
        assert row is not None
        assert row.status == "sent"
        assert row.attempts == 2
        assert gate.calls == 1
    finally:
        async with factory() as session, session.begin():
            await _wipe(
                session,
                strategy_id,
                version_id,
                league=880_521,
                home=880_522,
                away=880_523,
                fixture=880_524,
            )
        await engine.dispose()


class _Gate:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, body: dict[str, object]) -> None:
        self.calls += 1
        self.entered.set()
        await self.release.wait()


def _reply(fixture_id: int) -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "fixture_id": fixture_id,
                    "decision": "reject",
                    "estimated_probability": "0.60",
                    "confidence": "0.40",
                    "reasons": ["favorite_pressure"],
                    "rationale": "chasing the score",
                }
            ],
            "coupons": [],
        }
    )


def _request(strategy_id: int, version_id: int, fixture_id: int) -> CycleRequest:
    return CycleRequest(
        strategy_id=strategy_id,
        strategy_version_id=version_id,
        mode="sandbox",
        prompt_version="p1",
        prompt=LEAK,
        input_json={"note": LEAK},
        candidates=(
            CycleCandidate(
                fixture_id=fixture_id,
                period="regulation",
                goals_home=0,
                goals_away=1,
                ordinal=2,
                candidate_rule="favorite_losing",
                odd=Decimal("1.70"),
                n=1,
            ),
        ),
        open_fixture_ids=frozenset(),
        min_odd=Decimal("1.6"),
        max_legs=3,
    )


async def _seed(
    session,
    *,
    league: int = 880_501,
    home: int = 880_502,
    away: int = 880_503,
    fixture: int = 880_504,
    name: str = "advisor-langfuse",
) -> tuple[int, int]:
    session.add(League(id=league, name=name))
    session.add(Team(id=home, name="Home"))
    session.add(Team(id=away, name="Away"))
    await session.flush()
    session.add(
        Fixture(
            id=fixture,
            league_id=league,
            season=2026,
            home_team_id=home,
            away_team_id=away,
        )
    )
    strategy = AdvisorStrategy(name=name, kind="llm", status="sandbox")
    session.add(strategy)
    await session.flush()
    version = AdvisorStrategyVersion(
        strategy_id=strategy.id,
        version_no=1,
        prompt_version="p1",
        min_combined_odd=Decimal("1.600"),
        max_legs=3,
        max_minute_regulation=80,
        max_minute_extra=110,
        wait_minutes=10,
        candidate_rules=["favorite_losing"],
    )
    session.add(version)
    await session.flush()
    return version.id, strategy.id


async def _wipe(
    session,
    strategy_id: int,
    version_id: int,
    *,
    league: int = 880_501,
    home: int = 880_502,
    away: int = 880_503,
    fixture: int = 880_504,
) -> None:
    if strategy_id:
        await session.execute(
            delete(AdvisorDecision).where(AdvisorDecision.strategy_id == strategy_id)
        )
    if version_id:
        await session.execute(
            delete(AdvisorLangfuseOutbox).where(
                AdvisorLangfuseOutbox.cycle_id.in_(
                    select(AdvisorCycle.id).where(
                        AdvisorCycle.strategy_version_id == version_id
                    )
                )
            )
        )
        await session.execute(
            delete(AdvisorCycle).where(AdvisorCycle.strategy_version_id == version_id)
        )
        await session.execute(
            delete(AdvisorStrategyVersion).where(
                AdvisorStrategyVersion.id == version_id
            )
        )
    if strategy_id:
        await session.execute(
            delete(AdvisorStrategy).where(AdvisorStrategy.id == strategy_id)
        )
    await session.execute(delete(Fixture).where(Fixture.id == fixture))
    await session.execute(delete(Team).where(Team.id.in_((home, away))))
    await session.execute(delete(League).where(League.id == league))
