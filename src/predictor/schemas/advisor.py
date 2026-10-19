"""Settings and the strict JSON contract for one advisor model call."""

from __future__ import annotations

import math
from decimal import Decimal
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from predictor.schemas.settings import SettingsError
from predictor.services.advisor.recommend import parse_hours, parse_leagues

_ADVISOR_ENV: dict[str, str] = {
    "llm_provider": "LLM_PROVIDER",
    "llm_model": "LLM_MODEL",
    "llm_api_key": "LLM_API_KEY",
    "llm_timeout_seconds": "LLM_TIMEOUT_SECONDS",
    "llm_price_input_pln_per_mtok": "LLM_PRICE_INPUT_PLN_PER_MTOK",
    "llm_price_output_pln_per_mtok": "LLM_PRICE_OUTPUT_PLN_PER_MTOK",
    "advisor_daily_budget_pln": "ADVISOR_DAILY_BUDGET_PLN",
    "advisor_debounce_seconds": "ADVISOR_DEBOUNCE_SECONDS",
    "recommendation_stake": "ADVISOR_RECOMMENDATION_STAKE",
    "recommendation_loss_limit": "ADVISOR_RECOMMENDATION_LOSS_LIMIT",
    "operator_hours": "ADVISOR_OPERATOR_HOURS",
    "recommendation_leagues": "ADVISOR_RECOMMENDATION_LEAGUES",
    "recommendation_coupon_limit": "ADVISOR_RECOMMENDATION_COUPON_LIMIT",
    "recommendation_ttl_seconds": "ADVISOR_RECOMMENDATION_TTL_SECONDS",
    "withdraw_yield": "ADVISOR_WITHDRAW_YIELD",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "langfuse_public_key": "LANGFUSE_PUBLIC_KEY",
    "langfuse_secret_key": "LANGFUSE_SECRET_KEY",
    "langfuse_host": "LANGFUSE_HOST",
    "langfuse_timeout_seconds": "LANGFUSE_TIMEOUT_SECONDS",
}


class ReasonCode(StrEnum):
    FAVORITE_PRESSURE = "favorite_pressure"
    HIGH_SHOT_VOLUME = "high_shot_volume"
    LOW_TEMPO = "low_tempo"
    RED_CARD = "red_card"
    ODD_TOO_LOW = "odd_too_low"
    LATE_MINUTE = "late_minute"
    WEAK_FAVORITE = "weak_favorite"


class CandidateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: int
    decision: str
    estimated_probability: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    reasons: list[ReasonCode] = Field(min_length=1)
    rationale: str

    @field_validator("decision")
    @classmethod
    def _decision(cls, value: str) -> str:
        if value not in {"bet", "reject", "wait"}:
            raise ValueError("must be bet, reject, or wait")
        return value

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped


class CouponProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_ids: list[int] = Field(min_length=1)


class LlmDecision(BaseModel):
    """One model reply. Odds are not accepted: the database supplies them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: list[CandidateDecision]
    coupons: list[CouponProposal]


class AdvisorSettings(BaseSettings):
    """Read by predictor-advisor only. The ETL worker does not load this."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True, env_file=None)

    llm_provider: str = "google"
    llm_model: str = "gemini-2.5-flash"
    llm_api_key: SecretStr
    llm_timeout_seconds: float = 20
    llm_price_input_pln_per_mtok: Decimal
    llm_price_output_pln_per_mtok: Decimal
    advisor_daily_budget_pln: Decimal = Decimal("10")
    advisor_debounce_seconds: int = 20
    recommendation_stake: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ADVISOR_RECOMMENDATION_STAKE", "recommendation_stake"
        ),
    )
    recommendation_loss_limit: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ADVISOR_RECOMMENDATION_LOSS_LIMIT", "recommendation_loss_limit"
        ),
    )
    operator_hours: str = Field(
        default="",
        validation_alias=AliasChoices("ADVISOR_OPERATOR_HOURS", "operator_hours"),
    )
    recommendation_leagues: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ADVISOR_RECOMMENDATION_LEAGUES", "recommendation_leagues"
        ),
    )
    recommendation_coupon_limit: int = Field(
        default=10,
        validation_alias=AliasChoices(
            "ADVISOR_RECOMMENDATION_COUPON_LIMIT", "recommendation_coupon_limit"
        ),
    )
    recommendation_ttl_seconds: int = Field(
        default=180,
        validation_alias=AliasChoices(
            "ADVISOR_RECOMMENDATION_TTL_SECONDS", "recommendation_ttl_seconds"
        ),
    )
    withdraw_yield: Decimal = Field(
        default=Decimal("0"),
        validation_alias=AliasChoices("ADVISOR_WITHDRAW_YIELD", "withdraw_yield"),
    )
    telegram_bot_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
    )
    telegram_chat_id: str = Field(
        default="",
        validation_alias=AliasChoices("TELEGRAM_CHAT_ID", "telegram_chat_id"),
    )
    langfuse_public_key: SecretStr = SecretStr("")
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_timeout_seconds: float = 5

    @field_validator("llm_provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        folded = value.strip().casefold()
        if folded != "google":
            raise ValueError("must be google")
        return folded

    @field_validator("llm_model")
    @classmethod
    def _model(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("llm_api_key")
    @classmethod
    def _key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("llm_timeout_seconds")
    @classmethod
    def _timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0 or value > 120:
            raise ValueError("must be finite and between 0 and 120 seconds")
        return value

    @field_validator("langfuse_host")
    @classmethod
    def _langfuse_host(cls, value: str) -> str:
        stripped = value.strip().rstrip("/")
        parts = urlsplit(stripped)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError("must be an https origin without a path")
        port = f":{parts.port}" if parts.port else ""
        return f"https://{parts.hostname}{port}"

    @field_validator("operator_hours")
    @classmethod
    def _hours(cls, value: str) -> str:
        parse_hours(value)
        return value.strip()

    @field_validator("recommendation_leagues")
    @classmethod
    def _leagues(cls, value: str) -> str:
        parse_leagues(value)
        return value.strip()

    @field_validator("recommendation_coupon_limit")
    @classmethod
    def _coupon_limit(cls, value: int) -> int:
        if value < 1 or value > 100:
            raise ValueError("must be between 1 and 100")
        return value

    @field_validator("recommendation_ttl_seconds")
    @classmethod
    def _ttl(cls, value: int) -> int:
        if value < 30 or value > 600:
            raise ValueError("must be between 30 and 600 seconds")
        return value

    @field_validator("recommendation_stake", "recommendation_loss_limit", mode="before")
    @classmethod
    def _empty_optional_money(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("recommendation_stake", "recommendation_loss_limit")
    @classmethod
    def _optional_money(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        if not value.is_finite() or value <= 0:
            raise ValueError("must be finite and > 0")
        return value

    @field_validator("withdraw_yield")
    @classmethod
    def _withdraw(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("must be finite")
        return value

    @field_validator("advisor_debounce_seconds")
    @classmethod
    def _debounce(cls, value: int) -> int:
        if value < 15 or value > 30:
            raise ValueError("must be between 15 and 30 seconds")
        return value

    @field_validator("langfuse_timeout_seconds")
    @classmethod
    def _langfuse_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0 or value > 30:
            raise ValueError("must be finite and between 0 and 30 seconds")
        return value

    @field_validator(
        "llm_price_input_pln_per_mtok",
        "llm_price_output_pln_per_mtok",
        "advisor_daily_budget_pln",
    )
    @classmethod
    def _price(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("must be finite and > 0")
        return value


def load_advisor_settings() -> AdvisorSettings:
    try:
        return AdvisorSettings()
    except ValidationError as exc:
        names: list[str] = []
        seen: set[str] = set()
        for error in exc.errors():
            loc = error.get("loc") or ()
            key = str(loc[0]) if loc else "unknown"
            env_name = _ADVISOR_ENV.get(key, key.upper())
            if env_name not in seen:
                seen.add(env_name)
                names.append(env_name)
        listed = ", ".join(names) if names else "unknown fields"
        raise SettingsError(f"Missing or invalid configuration: {listed}") from exc
