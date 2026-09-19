from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, or_, select

from predictor.constants import CURSOR_KINDS, URGENT_PREMATCH_HORIZON_HOURS
from predictor.models.etl import EtlTask
from predictor.models.fixtures import Fixture
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.fixtures import FixtureItem
from predictor.schemas.settings import load_settings
from predictor.services.ingest.persist_fixtures import upsert_fixtures
from predictor.services.queue import (
    claim_next,
    claim_odds_task,
    claim_urgent_odds_task,
    claim_urgent_predictions_task,
    complete_task,
    enqueue_fixture_followups,
    ensure_cursors,
    ensure_prematch_for_fixture_ids,
    requeue_orphans,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
FID_SOON = 92001
FID_LATER = 92002
FID_FAR = 92003
FID_FT = 92004


@pytest.mark.asyncio
async def test_ensure_cursors_are_idempotent() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                await ensure_cursors(session)
        async with factory() as session:
            async with session.begin():
                await ensure_cursors(session)
        async with factory() as session:
            kinds = set(
                await session.scalars(
                    select(EtlTask.cursor_kind).where(EtlTask.cursor_kind.is_not(None))
                )
            )
        assert kinds == set(CURSOR_KINDS)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_checkpoint_rolls_back_with_open_transaction() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            try:
                async with session.begin():
                    task = EtlTask(
                        endpoint="/w2/rollback", params={}, status="in_progress"
                    )
                    session.add(task)
                    await session.flush()
                    await complete_task(session, task, "complete")
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
        async with factory() as session:
            leftover = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == "/w2/rollback")
            )
        assert leftover is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_complete_task_requires_transaction() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            task = EtlTask(endpoint="/w2/no-txn", params={}, status="pending")
            with pytest.raises(RuntimeError, match="transaction"):
                await complete_task(session, task, "complete")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_requeue_orphans_and_claim_skips_cursors() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        orphan_endpoint = f"/w2/orphan-{uuid.uuid4().hex[:8]}"
        claim_endpoint = f"/w2/claim-{uuid.uuid4().hex[:8]}"
        async with factory() as session:
            async with session.begin():
                await ensure_cursors(session)
                session.add(
                    EtlTask(endpoint=orphan_endpoint, params={}, status="in_progress")
                )
                session.add(
                    EtlTask(endpoint=claim_endpoint, params={}, status="pending")
                )
        async with factory() as session:
            async with session.begin():
                n = await requeue_orphans(session)
                assert n >= 1
                orphan = await session.scalar(
                    select(EtlTask).where(EtlTask.endpoint == orphan_endpoint)
                )
                assert orphan is not None
                assert orphan.status == "pending"
                assert orphan.last_error == "orphaned_in_progress"
                claimed_endpoints: list[str] = []
                parked: list[EtlTask] = []
                while True:
                    claimed = await claim_next(session)
                    if claimed is None:
                        break
                    claimed_endpoints.append(claimed.endpoint)
                    assert claimed.cursor_kind is None
                    if claimed.endpoint.startswith("/w2/"):
                        await complete_task(session, claimed, "complete")
                    else:
                        parked.append(claimed)
                for task in parked:
                    task.status = "pending"
                    task.started_at = None
                assert claim_endpoint in claimed_endpoints
                assert orphan_endpoint in claimed_endpoints
        async with factory() as session:
            orphan = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == orphan_endpoint)
            )
            done = await session.scalar(
                select(EtlTask).where(EtlTask.endpoint == claim_endpoint)
            )
        assert orphan is not None
        assert orphan.status == "complete"
        assert done is not None
        assert done.status == "complete"
        assert done.completed_at is not None
    finally:
        await engine.dispose()


def _fixture_payload(
    fixture_id: int,
    *,
    kickoff: datetime,
    status: str = "NS",
    home_id: int = 33,
    away_id: int = 34,
) -> dict:
    return {
        "fixture": {
            "id": fixture_id,
            "timezone": "UTC",
            "date": kickoff.isoformat().replace("+00:00", "+00:00"),
            "timestamp": int(kickoff.timestamp()),
            "periods": {"first": None, "second": None},
            "venue": {"id": 1, "name": "S", "city": "C"},
            "status": {
                "long": status,
                "short": status,
                "elapsed": None,
                "extra": None,
            },
        },
        "league": {
            "id": 39,
            "name": "PL",
            "country": "England",
            "logo": None,
            "flag": None,
            "season": 2026,
            "round": "R1",
        },
        "teams": {
            "home": {
                "id": home_id,
                "name": f"H{home_id}",
                "logo": None,
                "winner": None,
            },
            "away": {
                "id": away_id,
                "name": f"A{away_id}",
                "logo": None,
                "winner": None,
            },
        },
        "goals": {"home": None, "away": None},
        "score": {
            "halftime": {"home": None, "away": None},
            "fulltime": {"home": None, "away": None},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }


async def _purge_prematch_fixtures(session) -> None:
    ids = (FID_SOON, FID_LATER, FID_FAR, FID_FT)
    await session.execute(delete(EtlTask).where(EtlTask.fixture_id.in_(ids)))
    await session.execute(delete(Fixture).where(Fixture.id.in_(ids)))


@pytest.mark.asyncio
async def test_enqueue_followups_creates_odds_and_predictions() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                await _purge_prematch_fixtures(session)
                item = FixtureItem.model_validate(
                    _fixture_payload(FID_SOON, kickoff=NOW + timedelta(hours=2))
                )
                extra = await upsert_fixtures(session, [item])
                await enqueue_fixture_followups(session, extra)
        async with factory() as session:
            endpoints = set(
                await session.scalars(
                    select(EtlTask.endpoint).where(EtlTask.fixture_id == FID_SOON)
                )
            )
        assert "/predictions" in endpoints
        assert "/odds" in endpoints
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_prematch_creates_odds_task() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                await _purge_prematch_fixtures(session)
                item = FixtureItem.model_validate(
                    _fixture_payload(FID_LATER, kickoff=NOW + timedelta(hours=3))
                )
                await upsert_fixtures(session, [item])
                await ensure_prematch_for_fixture_ids(session, [FID_LATER])
        async with factory() as session:
            odds = await session.scalar(
                select(EtlTask).where(
                    EtlTask.endpoint == "/odds",
                    EtlTask.fixture_id == FID_LATER,
                )
            )
            pred = await session.scalar(
                select(EtlTask).where(
                    EtlTask.endpoint == "/predictions",
                    EtlTask.fixture_id == FID_LATER,
                )
            )
        assert odds is not None and odds.status == "pending"
        assert pred is not None and pred.status == "pending"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_odds_prefers_sooner_kickoff() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                await _purge_prematch_fixtures(session)
                soon = FixtureItem.model_validate(
                    _fixture_payload(
                        FID_SOON,
                        kickoff=NOW + timedelta(hours=1),
                        home_id=101,
                        away_id=102,
                    )
                )
                later = FixtureItem.model_validate(
                    _fixture_payload(
                        FID_LATER,
                        kickoff=NOW + timedelta(hours=5),
                        home_id=103,
                        away_id=104,
                    )
                )
                await upsert_fixtures(session, [soon, later])
                # Later kickoff gets lower task id (inserted first).
                session.add(
                    EtlTask(
                        endpoint="/odds",
                        fixture_id=FID_LATER,
                        params={"fixture": FID_LATER},
                        status="pending",
                    )
                )
                await session.flush()
                session.add(
                    EtlTask(
                        endpoint="/odds",
                        fixture_id=FID_SOON,
                        params={"fixture": FID_SOON},
                        status="pending",
                    )
                )
        async with factory() as session:
            async with session.begin():
                claimed = await claim_odds_task(session)
                assert claimed is not None
                assert claimed.fixture_id == FID_SOON
                await complete_task(session, claimed, "complete")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_urgent_skips_far_and_finished() -> None:
    settings = load_settings()
    engine = make_async_engine(settings)
    factory = make_session_factory(engine)
    ids = (FID_SOON, FID_FAR, FID_FT)
    parked: list[int] = []
    try:
        async with factory() as session:
            async with session.begin():
                await _purge_prematch_fixtures(session)
                near = FixtureItem.model_validate(
                    _fixture_payload(
                        FID_SOON,
                        kickoff=NOW + timedelta(hours=2),
                        home_id=201,
                        away_id=202,
                    )
                )
                far = FixtureItem.model_validate(
                    _fixture_payload(
                        FID_FAR,
                        kickoff=NOW
                        + timedelta(hours=URGENT_PREMATCH_HORIZON_HOURS + 2),
                        home_id=203,
                        away_id=204,
                    )
                )
                finished = FixtureItem.model_validate(
                    _fixture_payload(
                        FID_FT,
                        kickoff=NOW + timedelta(hours=1),
                        status="FT",
                        home_id=205,
                        away_id=206,
                    )
                )
                await upsert_fixtures(session, [near, far, finished])
                for fid in (FID_FAR, FID_FT, FID_SOON):
                    session.add(
                        EtlTask(
                            endpoint="/odds",
                            fixture_id=fid,
                            params={"fixture": fid},
                            status="pending",
                        )
                    )
                    session.add(
                        EtlTask(
                            endpoint="/predictions",
                            fixture_id=fid,
                            params={"fixture": fid},
                            status="pending",
                        )
                    )
                # Isolate from other pending odds/predictions in the shared DB.
                foreign = (
                    await session.scalars(
                        select(EtlTask).where(
                            EtlTask.endpoint.in_(("/odds", "/predictions")),
                            EtlTask.status.in_(("pending", "retryable_error")),
                            or_(
                                EtlTask.fixture_id.is_(None),
                                ~EtlTask.fixture_id.in_(ids),
                            ),
                        )
                    )
                ).all()
                for task in foreign:
                    parked.append(int(task.id))
                    task.status = "in_progress"
                    task.last_error = "test_park"
        async with factory() as session:
            async with session.begin():
                odds = await claim_urgent_odds_task(session, now=NOW)
                assert odds is not None
                assert odds.fixture_id == FID_SOON
                await complete_task(session, odds, "complete")
                pred = await claim_urgent_predictions_task(session, now=NOW)
                assert pred is not None
                assert pred.fixture_id == FID_SOON
                await complete_task(session, pred, "complete")
                assert await claim_urgent_odds_task(session, now=NOW) is None
                assert await claim_urgent_predictions_task(session, now=NOW) is None
    finally:
        async with factory() as session:
            async with session.begin():
                if parked:
                    for task in (
                        await session.scalars(
                            select(EtlTask).where(EtlTask.id.in_(parked))
                        )
                    ).all():
                        if task.last_error == "test_park":
                            task.status = "pending"
                            task.last_error = None
                            task.started_at = None
                await _purge_prematch_fixtures(session)
        await engine.dispose()
