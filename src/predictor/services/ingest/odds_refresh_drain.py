"""Worker drain for advisor_odds_refresh: LISTEN wake + targeted live persist."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime

import psycopg
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from predictor.client.football import FootballClient
from predictor.constants import ADVISOR_ODDS_REFRESH_CHANNEL
from predictor.logutil import log_json
from predictor.models.advisor import AdvisorOddsRefresh
from predictor.postgres import connect_kwargs
from predictor.schemas.settings import Settings
from predictor.services.ingest.candidate_refresh import persist_targeted_live

_SERVICE = "worker"


async def pending_odds_refresh_ids(
    session: AsyncSession,
) -> tuple[int, ...]:
    rows = await session.scalars(select(AdvisorOddsRefresh.fixture_id))
    return tuple(sorted(int(fid) for fid in rows))


async def clear_odds_refresh(session: AsyncSession, fixture_ids: Sequence[int]) -> None:
    ids = [int(fid) for fid in fixture_ids]
    if not ids:
        return
    await session.execute(
        delete(AdvisorOddsRefresh).where(AdvisorOddsRefresh.fixture_id.in_(ids))
    )


async def drain_odds_refresh(
    client: FootballClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
) -> int:
    """Pull pending fixtures, persist live rows, clear only on success."""
    async with session_factory() as session:
        ids = await pending_odds_refresh_ids(session)
    if not ids:
        return 0
    ok = await persist_targeted_live(
        client, session_factory, ids, now=now or datetime.now(UTC)
    )
    if not ok:
        return 0
    async with session_factory() as session, session.begin():
        await clear_odds_refresh(session, ids)
    return len(ids)


class OddsRefreshWake:
    """Dedicated LISTEN connection. Sets ``event`` when the advisor notifies."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn: psycopg.AsyncConnection[object] | None = None
        self._task: asyncio.Task[None] | None = None
        self.event = asyncio.Event()

    async def start(self) -> None:
        if self._conn is not None:
            raise RuntimeError("odds refresh wake already started")
        self._conn = await psycopg.AsyncConnection.connect(
            **connect_kwargs(self._settings),
            autocommit=True,
            application_name="predictor-worker-odds-refresh",
        )
        await self._conn.execute(f"LISTEN {ADVISOR_ODDS_REFRESH_CHANNEL}")
        self._task = asyncio.create_task(self._watch(), name="odds-refresh-listen")

    async def _watch(self) -> None:
        assert self._conn is not None
        try:
            async for _notify in self._conn.notifies():
                self.event.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            log_json(
                logging.WARNING,
                service=_SERVICE,
                event="odds_refresh_listen_failed",
            )

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        conn = self._conn
        self._conn = None
        if conn is not None:
            await conn.close()
