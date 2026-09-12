"""PostgreSQL connectivity helpers. No DSN is logged."""

from typing import TypedDict

import psycopg
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from predictor.schemas.settings import Settings


class ConnectKwargs(TypedDict):
    host: str
    port: int
    user: str
    password: str
    dbname: str


def connect_kwargs(settings: Settings) -> ConnectKwargs:
    return {
        "host": settings.postgres_host,
        "port": settings.postgres_port,
        "user": settings.postgres_user,
        "password": settings.postgres_password.get_secret_value(),
        "dbname": settings.postgres_db,
    }


def sqlalchemy_url(settings: Settings) -> URL:
    return URL.create(
        drivername="postgresql+psycopg",
        username=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )


def make_async_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        sqlalchemy_url(settings),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        echo=False,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def ping_postgres(settings: Settings) -> None:
    with psycopg.connect(**connect_kwargs(settings), connect_timeout=10) as conn:
        conn.execute("SELECT 1")
