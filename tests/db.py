"""Shared Postgres connection for integration tests."""

from __future__ import annotations

import os
from typing import Any

import psycopg
import pytest


def app_connect() -> psycopg.Connection[tuple[Any, ...]]:
    password = os.environ.get("PREDICTOR_DEV_PASSWORD") or os.environ.get(
        "POSTGRES_PASSWORD"
    )
    if not password:
        pytest.fail("PREDICTOR_DEV_PASSWORD or POSTGRES_PASSWORD is required")
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("POSTGRES_USER", "predictor_dev"),
        password=password,
        dbname=os.environ.get("POSTGRES_DB", "predictor_dev"),
        connect_timeout=5,
    )
