from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from predictor.api.main import create_app
from predictor.worker.main import main, run_worker


def test_health_ok(valid_env: None) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_app_exits_when_api_key_missing(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("API_SPORTS_KEY", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        create_app()
    assert exc_info.value.code == 1


def test_worker_main_exits_when_config_missing(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_run_worker_returns_when_stop_is_set(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("predictor.worker.main.ping_postgres", lambda settings: None)

    class ImmediateStop:
        def set(self) -> None:
            return None

        async def wait(self) -> None:
            return None

    monkeypatch.setattr("predictor.worker.main.asyncio.Event", ImmediateStop)
    await run_worker()
