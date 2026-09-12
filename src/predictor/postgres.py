"""PostgreSQL connectivity helpers. No DSN is logged."""

from typing import TypedDict

import psycopg

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


def ping_postgres(settings: Settings) -> None:
    with psycopg.connect(**connect_kwargs(settings), connect_timeout=10) as conn:
        conn.execute("SELECT 1")
