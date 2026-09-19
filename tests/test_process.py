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

    async def fake_settle(engine: object) -> int:
        del engine
        return 0

    monkeypatch.setattr("predictor.api.main.list_live_matches", fake_list)
    monkeypatch.setattr("predictor.api.main.settle_open_from_engine", fake_settle)
    with TestClient(create_app()) as client:
        html = client.get("/live")
        root = client.get("/")
        payload = client.get("/live.json")
    assert html.status_code == 200
    assert "text/html" in html.headers["content-type"]
    assert "Mecze na żywo" in html.text
    assert 'href="/live/next-goal"' in html.text
    assert 'href="/live/bets"' in html.text
    assert root.status_code == 200
    body = payload.json()
    assert payload.status_code == 200
    assert body["count"] == 0
    assert body["matches"] == []


def test_live_next_goal_html_and_json(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_next(engine: object) -> list[object]:
        del engine
        return []

    async def fake_settle(engine: object) -> int:
        del engine
        return 0

    async def fake_open(engine: object) -> dict[int, object]:
        del engine
        return {}

    async def fake_stake(engine: object) -> str:
        del engine
        return "21.00"

    monkeypatch.setattr("predictor.api.main.list_next_goal_matches", fake_next)
    monkeypatch.setattr("predictor.api.main.settle_open_from_engine", fake_settle)
    monkeypatch.setattr("predictor.api.main.settle_and_list_open", fake_open)
    monkeypatch.setattr("predictor.api.main.current_stake_label", fake_stake)
    with TestClient(create_app()) as client:
        html = client.get("/live/next-goal")
        payload = client.get("/live/next-goal.json")
    assert html.status_code == 200
    assert "Następny gol" in html.text
    assert 'href="/live/next-goal" class="active"' in html.text
    assert "faworyt przegrywa" in html.text
    body = payload.json()
    assert payload.status_code == 200
    assert body["count"] == 0
    assert body["matches"] == []
    assert body["current_stake"] == "21.00"


def test_live_bets_html_and_json(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_history(engine: object) -> dict[str, object]:
        del engine
        return {
            "generated_at": "2026-09-13T20:00:00+00:00",
            "metrics": {
                "n": 0,
                "wins": 0,
                "losses": 0,
                "voids": 0,
                "open_count": 0,
                "hit_rate": None,
                "saldo": "0.00",
                "current_stake": "21.00",
                "avg_odd": None,
                "staked": "0.00",
            },
            "bets": [],
        }

    monkeypatch.setattr("predictor.api.main.load_history_payload", fake_history)
    with TestClient(create_app()) as client:
        html = client.get("/live/bets")
        payload = client.get("/live/bets.json")
    assert html.status_code == 200
    assert "Zakłady next goal" in html.text
    assert 'href="/live/bets" class="active"' in html.text
    assert payload.status_code == 200
    assert payload.json()["metrics"]["current_stake"] == "21.00"


def test_place_and_settle_bet_json(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime
    from decimal import Decimal
    from types import SimpleNamespace

    match = SimpleNamespace(
        fixture_id=42,
        country="Test",
        league="L",
        home="H",
        away="A",
        goals_home=0,
        goals_away=0,
        elapsed=10,
        status_short="1H",
    )

    async def fake_next_goal(engine: object) -> list[tuple[object, object]]:
        del engine
        return [(match, None)]

    async def fake_settle_open(session: object) -> int:
        del session
        return 0

    placed = SimpleNamespace(
        id=7,
        fixture_id=42,
        odd=Decimal("1.700"),
        stake=Decimal("21.00"),
        status="open",
        placed_at=datetime(2026, 3, 1, tzinfo=UTC),
        settled_at=None,
        league="Test · L",
        home_name="H",
        away_name="A",
        goals_home=0,
        goals_away=0,
        payout_keep=Decimal("0.880"),
    )

    async def fake_place(session: object, **kwargs: object) -> object:
        del session, kwargs
        return placed

    settled = SimpleNamespace(**{**placed.__dict__, "status": "won"})
    settled.settled_at = datetime(2026, 3, 1, 1, tzinfo=UTC)

    async def fake_settle(session: object, bet_id: int, outcome: str) -> object:
        del session
        assert bet_id == 7
        assert outcome == "won"
        return settled

    class _SessionCM:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> object:
            return AsyncMockSession()

        async def __aexit__(self, *args: object) -> None:
            del args

    class AsyncMockSession:
        async def commit(self) -> None:
            return None

    monkeypatch.setattr("predictor.api.main.list_next_goal_matches", fake_next_goal)
    monkeypatch.setattr("predictor.api.main.settle_open_tickets", fake_settle_open)
    monkeypatch.setattr("predictor.api.main.place_bet", fake_place)
    monkeypatch.setattr("predictor.api.main.settle_bet_manual", fake_settle)
    monkeypatch.setattr("predictor.api.main.AsyncSession", _SessionCM)

    with TestClient(create_app()) as client:
        place = client.post(
            "/live/bets",
            data={"fixture_id": "42", "odd": "1.70"},
            headers={"Accept": "application/json"},
        )
        settle = client.post(
            "/live/bets/7/settle",
            data={"outcome": "won"},
            headers={"Accept": "application/json"},
        )
    assert place.status_code == 201
    assert place.json()["fixture_id"] == 42
    assert "selection" not in place.json()
    assert settle.status_code == 200
    assert settle.json()["status"] == "won"


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
