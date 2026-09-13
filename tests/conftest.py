from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator

import pytest

VALID_ENV: dict[str, str] = {
    "API_SPORTS_KEY": "test-api-key-not-real",
    "API_SPORTS_BASE_URL": "https://api.example.test",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_USER": "predictor_dev",
    "POSTGRES_PASSWORD": "test-db-password",
    "POSTGRES_DB": "predictor_dev",
    "QUOTA_DAILY_LIMIT": "7500",
    "QUOTA_SAFETY_BUFFER_PERCENT": "5",
    "INSTANCE_ID": "test-1",
    "HOST_ENVIRONMENT": "local",
    "PROMETHEUS_METRICS_TOKEN": "test-metrics-token",
    "LIVE_POLL_TARGET_SECONDS": "60",
}

SETTING_ENV_NAMES = tuple(VALID_ENV.keys())


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture
def valid_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in SETTING_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    for name, value in VALID_ENV.items():
        monkeypatch.setenv(name, value)
    yield
