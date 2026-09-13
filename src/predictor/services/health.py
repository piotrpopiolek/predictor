"""Operator health: read Postgres, Alembic head, and pg_locks. Never acquire."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from predictor.constants import IN_PLAY_FIXTURE_STATUSES, OPEN_ETL_STATUSES
from predictor.logutil import log_json
from predictor.models.etl import EtlRun, EtlTask
from predictor.models.fixtures import Fixture
from predictor.models.odds import FixtureOddsLive
from predictor.schemas.settings import Settings
from predictor.services.lock import HolderInfo, fetch_holder_sqlalchemy


def current_alembic_head(config_path: str = "alembic.ini") -> str:
    cfg = Config(config_path)
    script_location = cfg.get_main_option("script_location")
    if script_location is None:
        cfg.set_main_option("script_location", "alembic")
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:
        raise RuntimeError("alembic has no head revision")
    return head


async def inspect_ready(
    settings: Settings, engine: AsyncEngine
) -> tuple[dict[str, Any], bool]:
    payload: dict[str, Any] = {
        "ready": False,
        "postgres": False,
        "migrations": {"ok": False},
        "writer_lock": {"held": False, "backend_pid": None},
        "scheduler": {"run_open": False},
    }
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            payload["postgres"] = True
            version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            head = current_alembic_head()
            migrations_ok = version == head
            payload["migrations"] = {
                "current": version,
                "head": head,
                "ok": migrations_ok,
            }
            holder = await fetch_holder_sqlalchemy(conn)
            payload["writer_lock"] = _holder_payload(holder)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            open_run = await session.scalar(
                select(EtlRun.id)
                .where(EtlRun.lock_held.is_(True))
                .where(EtlRun.ended_at.is_(None))
                .order_by(EtlRun.started_at.desc())
                .limit(1)
            )
            payload["scheduler"] = {"run_open": open_run is not None}
    except Exception:
        log_json(logging.ERROR, service="status", event="ready_check_failed")
        payload["error"] = "postgres_unavailable"
        return payload, False

    lock_held = bool(payload["writer_lock"]["held"])
    scheduler_ok = bool(payload["scheduler"]["run_open"])
    ready = (
        bool(payload["postgres"])
        and bool(payload["migrations"]["ok"])
        and lock_held
        and scheduler_ok
    )
    payload["ready"] = ready
    payload["host_environment"] = settings.host_environment.value
    return payload, ready


async def operator_status(settings: Settings, engine: AsyncEngine) -> dict[str, Any]:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        holder = await fetch_holder_sqlalchemy(conn)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        run = await session.scalar(
            select(EtlRun).order_by(EtlRun.started_at.desc()).limit(1)
        )
        counts_rows = await session.execute(
            select(EtlTask.status, func.count()).group_by(EtlTask.status)
        )
        counts = {str(status): int(n) for status, n in counts_rows.all()}
        cursor_rows = await session.scalars(
            select(EtlTask)
            .where(EtlTask.cursor_kind.is_not(None))
            .order_by(EtlTask.cursor_kind)
        )
        cursors = [
            {
                "kind": task.cursor_kind,
                "status": task.status,
                "day_utc": (
                    task.day_utc.isoformat() if task.day_utc is not None else None
                ),
                "endpoint": task.endpoint,
            }
            for task in cursor_rows
        ]
    return {
        "lock": _holder_payload(holder),
        "run": _run_payload(run),
        "cursors": cursors,
        "queue": counts,
        "quota": None,
        "host_environment": settings.host_environment.value,
        "instance_id": settings.instance_id,
    }


async def task_counts(engine: AsyncEngine) -> dict[str, int]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        rows = await session.execute(
            select(EtlTask.status, func.count()).group_by(EtlTask.status)
        )
        return {str(status): int(n) for status, n in rows.all()}


async def scrape_gauges(engine: AsyncEngine) -> dict[str, float]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        in_play = await session.scalar(
            select(func.count())
            .select_from(Fixture)
            .where(Fixture.status_short.in_(tuple(IN_PLAY_FIXTURE_STATUSES)))
        )
        last_live = await session.scalar(select(func.max(FixtureOddsLive.captured_at)))
        oldest = await session.scalar(
            select(func.min(EtlTask.created_at)).where(
                EtlTask.status.in_(tuple(OPEN_ETL_STATUSES)),
                EtlTask.cursor_kind.is_(None),
            )
        )
    live_ts = 0.0
    if last_live is not None:
        live_ts = float(last_live.timestamp())
    age = 0.0
    if oldest is not None:
        now = datetime.now(UTC)
        created = oldest
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age = max(0.0, (now - created).total_seconds())
    return {
        "in_play_fixtures": float(in_play or 0),
        "live_last_snapshot_unixtime": live_ts,
        "oldest_pending_age_seconds": age,
    }


def _holder_payload(holder: HolderInfo | None) -> dict[str, Any]:
    if holder is None:
        return {"held": False, "backend_pid": None, "application_name": None}
    return {
        "held": True,
        "backend_pid": holder.backend_pid,
        "application_name": holder.application_name,
    }


def _run_payload(run: EtlRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "host": run.host,
        "host_environment": run.host_environment,
        "instance_id": run.instance_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "lock_held": run.lock_held,
        "postgres_backend_pid": run.postgres_backend_pid,
    }
