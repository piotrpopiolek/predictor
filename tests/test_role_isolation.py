"""T008: developer role must not CONNECT to predictor_prod."""

from __future__ import annotations

import os

import psycopg
import pytest


def test_dev_role_cannot_connect_to_predictor_prod() -> None:
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    password = os.environ.get("PREDICTOR_DEV_PASSWORD") or os.environ.get(
        "POSTGRES_PASSWORD"
    )
    if not password:
        pytest.fail("PREDICTOR_DEV_PASSWORD or POSTGRES_PASSWORD is required for T008")

    with pytest.raises(psycopg.OperationalError) as exc_info:
        psycopg.connect(
            host=host,
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            dbname="predictor_prod",
            user="predictor_dev",
            password=password,
            connect_timeout=5,
        )

    message = str(exc_info.value).lower()
    assert "permission denied" in message, message
