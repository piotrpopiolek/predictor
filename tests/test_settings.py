from __future__ import annotations

import pytest

from predictor.logutil import configure_logging, log_startup
from predictor.schemas.settings import SettingsError, load_settings, startup_log_fields


def test_load_settings_reads_env(valid_env: None) -> None:
    settings = load_settings()
    assert settings.postgres_db == "predictor_dev"
    assert settings.postgres_user == "predictor_dev"
    assert settings.host_environment.value == "local"
    assert settings.quota_safety_buffer_percent == 5.0
    assert settings.api_sports_key.get_secret_value() == "test-api-key-not-real"


def test_missing_api_key_names_variable(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("API_SPORTS_KEY", raising=False)
    with pytest.raises(SettingsError, match="API_SPORTS_KEY"):
        load_settings()


def test_missing_api_base_url_names_variable(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("API_SPORTS_BASE_URL", raising=False)
    with pytest.raises(SettingsError, match="API_SPORTS_BASE_URL"):
        load_settings()


def test_load_settings_reads_base_url_from_env(valid_env: None) -> None:
    settings = load_settings()
    assert settings.api_sports_base_url == "https://api.example.test"


def test_missing_postgres_password_names_variable(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with pytest.raises(SettingsError, match="POSTGRES_PASSWORD"):
        load_settings()


def test_empty_api_key_is_invalid(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_SPORTS_KEY", "   ")
    with pytest.raises(SettingsError, match="API_SPORTS_KEY"):
        load_settings()


def test_invalid_host_environment(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOST_ENVIRONMENT", "staging")
    with pytest.raises(SettingsError, match="HOST_ENVIRONMENT"):
        load_settings()


def test_startup_fields_omit_secrets(valid_env: None) -> None:
    settings = load_settings()
    fields = startup_log_fields(settings)
    blob = " ".join(fields.values())
    assert "test-api-key-not-real" not in blob
    assert "test-db-password" not in blob
    assert "test-metrics-token" not in blob
    assert fields["database"] == "predictor_dev"
    assert fields["role"] == "predictor_dev"


def test_startup_log_omits_secrets(
    valid_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    settings = load_settings()
    log_startup(settings, service="worker")
    text = capsys.readouterr().out
    assert "test-api-key-not-real" not in text
    assert "test-db-password" not in text
    assert "predictor_dev" in text
    assert "worker" in text


def test_quota_buffer_out_of_range(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUOTA_SAFETY_BUFFER_PERCENT", "101")
    with pytest.raises(SettingsError, match="QUOTA_SAFETY_BUFFER_PERCENT"):
        load_settings()


def test_empty_postgres_user_is_invalid(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POSTGRES_USER", "  ")
    with pytest.raises(SettingsError, match="POSTGRES_USER"):
        load_settings()


def test_quota_limit_must_be_positive(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUOTA_DAILY_LIMIT", "0")
    with pytest.raises(SettingsError, match="QUOTA_DAILY_LIMIT"):
        load_settings()


def test_live_interval_must_be_positive(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIVE_POLL_TARGET_SECONDS", "0")
    with pytest.raises(SettingsError, match="LIVE_POLL_TARGET_SECONDS"):
        load_settings()


def test_postgres_port_range(valid_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PORT", "70000")
    with pytest.raises(SettingsError, match="POSTGRES_PORT"):
        load_settings()


def test_does_not_sniff_dsn(valid_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://predictor_app:secret@db/predictor_prod",
    )
    settings = load_settings()
    assert settings.postgres_db == "predictor_dev"
    assert settings.postgres_user == "predictor_dev"
