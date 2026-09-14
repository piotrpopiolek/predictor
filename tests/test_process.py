from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from predictor.api.main import create_app
from predictor.services.lock import LockBusyError
from predictor.worker.main import main, run_worker


class _FakeLock:
    def __init__(self, acquired: bool, holder_pid: int = 99) -> None:
        self._acquired = acquired
        self.backend_pid = 1 if acquired else None
        self.closed = False
        self.released = False
        self._holder_pid = holder_pid

    async def try_acquire(self) -> bool:
        return self._acquired

    async def holder_info(self) -> object:
        from predictor.services.lock import HolderInfo

        return HolderInfo(
            backend_pid=self._holder_pid, application_name="predictor-worker-lock"
        )

    async def release(self) -> None:
        self.released = True

    async def close(self) -> None:
        self.closed = True


def test_health_ok(valid_env: None) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_live_board_html_and_json(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list(engine: object) -> list[object]:
        del engine
        return []

    monkeypatch.setattr("predictor.api.main.list_live_matches", fake_list)
    with TestClient(create_app()) as client:
        html = client.get("/live")
        root = client.get("/")
        payload = client.get("/live.json")
    assert html.status_code == 200
    assert "text/html" in html.headers["content-type"]
    assert "Mecze na żywo" in html.text
    assert root.status_code == 200
    body = payload.json()
    assert payload.status_code == 200
    assert body["count"] == 0
    assert body["matches"] == []


def test_metrics_unauthorized_without_token(valid_env: None) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/metrics")
    assert response.status_code == 401


def test_metrics_unauthorized_wrong_token(valid_env: None) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


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


def test_worker_main_exits_when_lock_busy(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom() -> None:
        raise LockBusyError("writer lock 739201 is held by backend pid 42")

    monkeypatch.setattr("predictor.worker.main.run_worker", boom)
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_run_worker_returns_when_stop_is_set(valid_env: None) -> None:
    stop = asyncio.Event()
    stop.set()
    lock = _FakeLock(acquired=True)

    async def noop_loop(settings: object, held: object, event: asyncio.Event) -> None:
        await event.wait()

    await run_worker(lock=lock, stop=stop, locked_loop=noop_loop)  # type: ignore[arg-type]
    assert lock.released
    assert lock.closed


@pytest.mark.asyncio
async def test_run_worker_no_locked_loop_without_lock(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = False

    async def boom_loop(settings: object, held: object, event: asyncio.Event) -> None:
        nonlocal entered
        entered = True

    def boom_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("FootballClient must not be constructed without the lock")

    monkeypatch.setattr("predictor.worker.main.FootballClient", boom_client)
    lock = _FakeLock(acquired=False)
    with pytest.raises(LockBusyError, match="99"):
        await run_worker(lock=lock, locked_loop=boom_loop)  # type: ignore[arg-type]
    assert entered is False
    assert lock.closed
