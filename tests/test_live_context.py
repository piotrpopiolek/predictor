from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, or_, select
from tenacity import RetryCallState
from tenacity.wait import wait_base

from predictor.client.football import FootballClient
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.fixtures import FixtureItem
from predictor.schemas.settings import load_settings
from predictor.services.ingest.enrichment import EnrichmentIngest
from predictor.services.ingest.fixtures import FixtureIngest
from predictor.services.ingest.global_entities import GlobalIngest
from predictor.services.ingest.live_context import (
    LiveContextIngest,
    context_budget_left,
)
from predictor.services.ingest.persist_fixtures import upsert_fixtures
from predictor.services.ingest.prematch import PrematchIngest
from tests.test_queue import _fixture_payload

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=UTC)
FID_LIVE = 93021
FID_OTHER = 93022


class WaitZero(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        return 0.0


def test_context_budget_stops_on_calls_time_and_quota() -> None:
    assert (
        context_budget_left(
            calls=0,
            max_calls=16,
            elapsed=10,
            target=60,
            fraction=0.85,
            quota_left=True,
        )
        is True
    )
    assert (
        context_budget_left(
            calls=16,
            max_calls=16,
            elapsed=1,
            target=60,
            fraction=0.85,
            quota_left=True,
        )
        is False
    )
    assert (
        context_budget_left(
            calls=1,
            max_calls=16,
            elapsed=51,
            target=60,
            fraction=0.85,
            quota_left=True,
        )
        is False
    )
    assert (
        context_budget_left(
            calls=0,
            max_calls=16,
            elapsed=0,
            target=60,
            fraction=0.85,
            quota_left=False,
        )
        is False
    )


@pytest.mark.asyncio
async def test_live_context_prefers_live_fixture_and_retries_empty_odds() -> None:
    settings = load_settings()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "get": "odds",
                "errors": [],
                "results": 0,
                "paging": {"current": 1, "total": 1},
                "response": [],
            },
        )

    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    client = FootballClient(
        settings,
        locked=True,
        transport=httpx.MockTransport(handler),
        retry_wait=WaitZero(),
    )
    try:
        async with factory() as session:
            async with session.begin():
                await _purge(session)
                live = FixtureItem.model_validate(
                    _fixture_payload(
                        FID_LIVE,
                        kickoff=NOW,
                        status="1H",
                        home_id=401,
                        away_id=402,
                    )
                )
                other = FixtureItem.model_validate(
                    _fixture_payload(
                        FID_OTHER,
                        kickoff=NOW - timedelta(hours=2),
                        status="FT",
                        home_id=403,
                        away_id=404,
                    )
                )
                await upsert_fixtures(session, [other, live])
                session.add(
                    EtlTask(
                        endpoint="/odds",
                        fixture_id=FID_OTHER,
                        params={"fixture": FID_OTHER},
                        status="pending",
                    )
                )
        context = LiveContextIngest(
            factory,
            enrichment=EnrichmentIngest(client, factory, now_fn=lambda: NOW),
            prematch=PrematchIngest(client, factory, now_fn=lambda: NOW),
            global_ingest=GlobalIngest(client, factory, now_fn=lambda: NOW),
            fixtures=FixtureIngest(client, factory, now_fn=lambda: NOW),
            target_seconds=60,
            max_calls=1,
            now_fn=lambda: NOW,
        )
        await context.refresh([FID_LIVE])
        assert all(f"fixture={FID_OTHER}" not in url for url in seen)
        async with factory() as session:
            live_odds = await session.scalar(
                select(EtlTask).where(
                    EtlTask.endpoint == "/odds",
                    EtlTask.fixture_id == FID_LIVE,
                )
            )
            other_odds = await session.scalar(
                select(EtlTask).where(
                    EtlTask.endpoint == "/odds",
                    EtlTask.fixture_id == FID_OTHER,
                )
            )
            live_pred = await session.scalar(
                select(EtlTask).where(
                    EtlTask.endpoint == "/predictions",
                    EtlTask.fixture_id == FID_LIVE,
                )
            )
        assert live_odds is not None
        assert live_odds.status == "retryable_error"
        assert live_odds.params.get("next_attempt_at")
        if seen:
            assert any(f"fixture={FID_LIVE}" in url for url in seen)
        else:
            assert live_odds.params.get("reason") == "coverage_false"
        assert other_odds is not None
        assert other_odds.status == "pending"
        assert live_pred is not None
        assert live_pred.status == "pending"
    finally:
        async with factory() as session:
            async with session.begin():
                await _purge(session)
        await client.aclose()
        await engine.dispose()


async def _purge(session) -> None:
    await session.execute(
        delete(EtlTask).where(
            or_(
                EtlTask.fixture_id.in_((FID_LIVE, FID_OTHER)),
                EtlTask.params.contains({"h2h": "401-402"}),
                EtlTask.params.contains({"h2h": "403-404"}),
            )
        )
    )
    await session.execute(delete(Fixture).where(Fixture.id.in_((FID_LIVE, FID_OTHER))))
