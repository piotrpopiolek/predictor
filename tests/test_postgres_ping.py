from __future__ import annotations

from typing import Any

import pytest

from predictor.postgres import ping_postgres
from predictor.schemas.settings import load_settings


class _FakeResult:
    pass


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: object) -> _FakeResult:
        assert query == "SELECT 1"
        return _FakeResult()


def test_ping_postgres_executes_select(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_connect(**kwargs: Any) -> _FakeConn:
        captured.update(kwargs)
        return _FakeConn()

    monkeypatch.setattr("predictor.postgres.psycopg.connect", fake_connect)
    ping_postgres(load_settings())
    assert captured["user"] == "predictor_dev"
    assert captured["dbname"] == "predictor_dev"
    assert captured["password"] == "test-db-password"
    assert captured["connect_timeout"] == 10
