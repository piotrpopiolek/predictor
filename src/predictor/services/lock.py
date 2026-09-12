"""Session advisory lock for the single writer (FR-039). Not xact, not etl_lease."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncConnection

from predictor.constants import (
    LOCK_APPLICATION_NAME,
    LOCK_KEEPALIVES_COUNT,
    LOCK_KEEPALIVES_IDLE_SECONDS,
    LOCK_KEEPALIVES_INTERVAL_SECONDS,
    WRITER_LOCK_KEY,
)
from predictor.postgres import connect_kwargs
from predictor.schemas.settings import Settings


class LockBusyError(RuntimeError):
    """Writer lock is held by another backend. No API requests may follow."""


@dataclass(frozen=True, slots=True)
class HolderInfo:
    backend_pid: int
    application_name: str | None = None
    usename: str | None = None


def advisory_lock_parts(key: int = WRITER_LOCK_KEY) -> tuple[int, int]:
    """Split bigint advisory key the way pg_locks stores it (classid, objid)."""
    return (key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF


def _holder_sql_psycopg() -> str:
    return """
        SELECT l.pid, a.application_name, a.usename
        FROM pg_locks l
        LEFT JOIN pg_stat_activity a ON a.pid = l.pid
        WHERE l.locktype = 'advisory'
          AND l.classid = %s
          AND l.objid = %s
          AND l.objsubid = 1
          AND l.granted
          AND l.database = (
                SELECT oid FROM pg_database WHERE datname = current_database()
          )
        """


def lock_lookup_sql() -> TextClause:
    """Read-only pg_locks lookup. Never acquires the writer lock."""
    return text("""
        SELECT l.pid, a.application_name, a.usename
        FROM pg_locks l
        LEFT JOIN pg_stat_activity a ON a.pid = l.pid
        WHERE l.locktype = 'advisory'
          AND l.classid = :classid
          AND l.objid = :objid
          AND l.objsubid = 1
          AND l.granted
          AND l.database = (
                SELECT oid FROM pg_database WHERE datname = current_database()
          )
        """)


def lock_busy_message(info: HolderInfo | None) -> str:
    if info is None:
        return f"writer lock {WRITER_LOCK_KEY} is held by another backend"
    app = info.application_name or "unknown"
    return (
        f"writer lock {WRITER_LOCK_KEY} is held by backend pid {info.backend_pid} "
        f"(application_name={app})"
    )


def _row_to_holder(row: tuple[Any, ...] | None) -> HolderInfo | None:
    if row is None or row[0] is None:
        return None
    return HolderInfo(
        backend_pid=int(row[0]),
        application_name=str(row[1]) if row[1] is not None else None,
        usename=str(row[2]) if row[2] is not None else None,
    )


async def fetch_holder_psycopg(conn: psycopg.AsyncConnection[Any]) -> HolderInfo | None:
    classid, objid = advisory_lock_parts()
    cursor = await conn.execute(_holder_sql_psycopg(), (classid, objid))
    row = await cursor.fetchone()
    return _row_to_holder(row)


async def fetch_holder_sqlalchemy(conn: AsyncConnection) -> HolderInfo | None:
    classid, objid = advisory_lock_parts()
    result = await conn.execute(lock_lookup_sql(), {"classid": classid, "objid": objid})
    row = result.first()
    if row is None:
        return None
    return _row_to_holder((row[0], row[1], row[2]))


class WriterLock:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn: psycopg.AsyncConnection[Any] | None = None
        self._held = False
        self._backend_pid: int | None = None

    @property
    def held(self) -> bool:
        return self._held

    @property
    def backend_pid(self) -> int | None:
        return self._backend_pid

    async def try_acquire(self) -> bool:
        if self._conn is not None:
            raise RuntimeError("lock connection already open")
        self._conn = await psycopg.AsyncConnection.connect(
            **connect_kwargs(self._settings),
            autocommit=True,
            application_name=LOCK_APPLICATION_NAME,
            keepalives=1,
            keepalives_idle=LOCK_KEEPALIVES_IDLE_SECONDS,
            keepalives_interval=LOCK_KEEPALIVES_INTERVAL_SECONDS,
            keepalives_count=LOCK_KEEPALIVES_COUNT,
        )
        pid_row = await (await self._conn.execute("SELECT pg_backend_pid()")).fetchone()
        if pid_row is None:
            await self.close()
            raise RuntimeError("could not read pg_backend_pid")
        self._backend_pid = int(pid_row[0])
        lock_row = await (
            await self._conn.execute(
                "SELECT pg_try_advisory_lock(%s)", (WRITER_LOCK_KEY,)
            )
        ).fetchone()
        self._held = bool(lock_row[0]) if lock_row is not None else False
        return self._held

    async def holder_info(self) -> HolderInfo | None:
        if self._conn is None:
            raise RuntimeError("lock connection is closed")
        return await fetch_holder_psycopg(self._conn)

    async def release(self) -> None:
        if self._conn is None or not self._held:
            return
        await self._conn.execute("SELECT pg_advisory_unlock(%s)", (WRITER_LOCK_KEY,))
        self._held = False

    async def close(self) -> None:
        if self._conn is None:
            return
        conn = self._conn
        self._conn = None
        self._held = False
        await conn.close()
