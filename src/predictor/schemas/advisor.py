"""Settings and the strict JSON contract for one advisor model call."""

from __future__ import annotations

import math
from decimal import Decimal
from enum import StrEnum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from predictor.schemas.settings import SettingsError

_ADVISOR_ENV: dict[str, str] = {
    "llm_provider": "LLM_PROVIDER",
    "llm_model": "LLM_MODEL",
    "llm_api_key": "LLM_API_KEY",
    "llm_timeout_seconds": "LLM_TIMEOUT_SECONDS",
    "llm_price_input_pln_per_mtok": "LLM_PRICE_INPUT_PLN_PER_MTOK",
    "llm_price_output_pln_per_mtok": "LLM_PRICE_OUTPUT_PLN_PER_MTOK",
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

    @field_validator("llm_price_input_pln_per_mtok", "llm_price_output_pln_per_mtok")
    @classmethod
    def _price(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("must be > 0")
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
