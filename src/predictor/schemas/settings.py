"""Pydantic Settings for Predictor Data Mirror.

Environment names are the public contract. Missing values abort
startup with those names. There is no writer-disable flag and no DSN sniffing.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsError(Exception):
    """Raised when required configuration is missing or invalid."""


class HostEnvironment(StrEnum):
    LOCAL = "local"
    VPS = "vps"


FIELD_TO_ENV: dict[str, str] = {
    "api_sports_key": "API_SPORTS_KEY",
    "api_sports_base_url": "API_SPORTS_BASE_URL",
    "postgres_host": "POSTGRES_HOST",
    "postgres_port": "POSTGRES_PORT",
    "postgres_user": "POSTGRES_USER",
    "postgres_password": "POSTGRES_PASSWORD",
    "postgres_db": "POSTGRES_DB",
    "quota_daily_limit": "QUOTA_DAILY_LIMIT",
    "quota_safety_buffer_percent": "QUOTA_SAFETY_BUFFER_PERCENT",
    "instance_id": "INSTANCE_ID",
    "host_environment": "HOST_ENVIRONMENT",
    "prometheus_metrics_token": "PROMETHEUS_METRICS_TOKEN",
    "live_poll_target_seconds": "LIVE_POLL_TARGET_SECONDS",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
        frozen=True,
        env_file=None,
    )

    api_sports_key: SecretStr
    api_sports_base_url: str
    postgres_host: str
    postgres_port: int = 5432
    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    quota_daily_limit: int
    quota_safety_buffer_percent: float = 3.0
    instance_id: str
    host_environment: HostEnvironment
    prometheus_metrics_token: SecretStr
    live_poll_target_seconds: int = 60

    @field_validator(
        "api_sports_key",
        "postgres_password",
        "prometheus_metrics_token",
        mode="after",
    )
    @classmethod
    def _secret_non_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty")
        return value

    @field_validator(
        "api_sports_base_url",
        "postgres_user",
        "postgres_db",
        "instance_id",
        "postgres_host",
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("quota_daily_limit")
    @classmethod
    def _positive_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be >= 1")
        return value

    @field_validator("quota_safety_buffer_percent")
    @classmethod
    def _buffer_range(cls, value: float) -> float:
        if value < 0 or value > 100:
            raise ValueError("must be between 0 and 100")
        return value

    @field_validator("live_poll_target_seconds")
    @classmethod
    def _positive_interval(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be >= 1")
        return value

    @field_validator("postgres_port")
    @classmethod
    def _valid_port(cls, value: int) -> int:
        if value < 1 or value > 65535:
            raise ValueError("must be between 1 and 65535")
        return value


def _env_names_from_validation_error(exc: ValidationError) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for error in exc.errors():
        loc = error.get("loc") or ()
        key = str(loc[0]) if loc else "unknown"
        env_name = FIELD_TO_ENV.get(key, key.upper())
        if env_name not in seen:
            seen.add(env_name)
            names.append(env_name)
    return names


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        names = _env_names_from_validation_error(exc)
        listed = ", ".join(names) if names else "unknown fields"
        raise SettingsError(f"Missing or invalid configuration: {listed}") from exc


def startup_log_fields(settings: Settings) -> dict[str, str]:
    """Safe connection identity for logs: database and role, never password or DSN."""
    return {
        "database": settings.postgres_db,
        "role": settings.postgres_user,
        "host_environment": settings.host_environment.value,
        "instance_id": settings.instance_id,
    }
