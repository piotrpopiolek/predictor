"""LLM contract: strict schema, semantic checks, one Gemini call, no network."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from predictor.schemas.advisor import AdvisorSettings, load_advisor_settings
from predictor.schemas.settings import Settings, SettingsError
from predictor.services.advisor.llm import review_model_output
from predictor.services.advisor.llm.gemini import GeminiProvider
from predictor.services.advisor.llm.protocol import ProviderError
from predictor.services.advisor.llm.validate import PricedCandidate, Verdict

MIN = Decimal("1.6")
ODD = Decimal("1.70")


def _row(
    fixture_id: int,
    decision: str = "reject",
    *,
    probability: str = "0.55",
    confidence: str = "0.40",
) -> dict[str, object]:
    return {
        "fixture_id": fixture_id,
        "decision": decision,
        "estimated_probability": probability,
        "confidence": confidence,
        "reasons": ["favorite_pressure"],
        "rationale": "the favourite is chasing",
    }


def _raw(candidates: list[dict[str, object]], coupons: list[list[int]]) -> str:
    return json.dumps(
        {
            "candidates": candidates,
            "coupons": [{"fixture_ids": ids} for ids in coupons],
        }
    )


def _playable(*fixture_ids: int) -> list[PricedCandidate]:
    return [PricedCandidate(fixture_id, ODD) for fixture_id in fixture_ids]


def _assert_no_bet(verdict: Verdict, detail: str) -> None:
    assert verdict.status == "invalid_output"
    assert verdict.coupons == ()
    assert verdict.detail == detail


def test_worker_settings_do_not_require_an_llm_key() -> None:
    assert "llm_api_key" not in Settings.model_fields


def test_advisor_settings_name_the_missing_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "LLM_API_KEY",
        "LLM_PRICE_INPUT_PLN_PER_MTOK",
        "LLM_PRICE_OUTPUT_PLN_PER_MTOK",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SettingsError, match="LLM_API_KEY"):
        load_advisor_settings()


def test_advisor_settings_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("LLM_PRICE_INPUT_PLN_PER_MTOK", "0.50")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PLN_PER_MTOK", "1.25")
    settings = load_advisor_settings()
    assert isinstance(settings, AdvisorSettings)
    assert settings.llm_provider == "google"
    assert settings.llm_model == "gemini-2.5-flash"
    assert settings.llm_timeout_seconds == 20
    assert settings.llm_api_key.get_secret_value() == "secret"


def test_nan_timeout_is_rejected_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("LLM_PRICE_INPUT_PLN_PER_MTOK", "0.50")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PLN_PER_MTOK", "1.25")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "nan")
    with pytest.raises(SettingsError, match="LLM_TIMEOUT_SECONDS"):
        load_advisor_settings()


def test_valid_coupon_uses_database_odds_not_the_reply() -> None:
    raw = _raw(
        [_row(1, "bet"), _row(2, "reject")],
        [[1]],
    )
    verdict = review_model_output(raw, _playable(1, 2), set(), min_odd=MIN, max_legs=3)
    assert verdict.status == "ok"
    assert verdict.coupons[0].fixture_ids == (1,)
    assert verdict.coupons[0].combined_odd == ODD


def test_schema_failures_are_invalid_output_and_place_nothing() -> None:
    playable = _playable(1)
    _assert_no_bet(
        review_model_output("nope", playable, set(), min_odd=MIN, max_legs=3),
        "not_json",
    )
    fenced = "```json\n" + _raw([_row(1, "reject")], []) + "\n```"
    _assert_no_bet(
        review_model_output(fenced, playable, set(), min_odd=MIN, max_legs=3),
        "not_json",
    )
    extra = json.loads(_raw([_row(1, "reject")], []))
    extra["candidates"][0]["odd"] = "1.90"
    _assert_no_bet(
        review_model_output(
            json.dumps(extra), playable, set(), min_odd=MIN, max_legs=3
        ),
        "schema",
    )
    unknown = json.loads(_raw([_row(1, "reject")], []))
    unknown["candidates"][0]["reasons"] = ["because"]
    _assert_no_bet(
        review_model_output(
            json.dumps(unknown), playable, set(), min_odd=MIN, max_legs=3
        ),
        "schema",
    )


def test_every_candidate_needs_exactly_one_decision() -> None:
    missing = review_model_output(
        _raw([_row(1, "reject")], []),
        _playable(1, 2),
        set(),
        min_odd=MIN,
        max_legs=3,
    )
    _assert_no_bet(missing, "candidate_set")
    duplicate = review_model_output(
        _raw([_row(1, "reject"), _row(1, "wait")], []),
        _playable(1),
        set(),
        min_odd=MIN,
        max_legs=3,
    )
    _assert_no_bet(duplicate, "duplicate_candidate")


def test_coupon_rules_reject_the_whole_cycle() -> None:
    unknown = review_model_output(
        _raw([_row(1, "bet"), _row(2, "reject")], [[9]]),
        _playable(1, 2),
        set(),
        min_odd=MIN,
        max_legs=3,
    )
    _assert_no_bet(unknown, "unknown_fixture")
    opened = review_model_output(
        _raw([_row(1, "bet")], [[1]]),
        _playable(1),
        {1},
        min_odd=MIN,
        max_legs=3,
    )
    _assert_no_bet(opened, "open_coupon")
    twice = review_model_output(
        _raw(
            [_row(1, "bet"), _row(2, "bet"), _row(3, "reject")],
            [[1, 2], [2]],
        ),
        _playable(1, 2, 3),
        set(),
        min_odd=MIN,
        max_legs=3,
    )
    _assert_no_bet(twice, "duplicate_leg")
    low = review_model_output(
        _raw([_row(1, "bet")], [[1]]),
        _playable(1),
        set(),
        min_odd=Decimal("2.0"),
        max_legs=3,
    )
    _assert_no_bet(low, "below_threshold")
    too_many = review_model_output(
        _raw(
            [_row(1, "bet"), _row(2, "bet"), _row(3, "bet"), _row(4, "reject")],
            [[1, 2, 3, 4]],
        ),
        _playable(1, 2, 3, 4),
        set(),
        min_odd=Decimal("1.0"),
        max_legs=3,
    )
    _assert_no_bet(too_many, "too_many_legs")
    orphan = review_model_output(
        _raw([_row(1, "bet"), _row(2, "reject")], []),
        _playable(1, 2),
        set(),
        min_odd=MIN,
        max_legs=3,
    )
    _assert_no_bet(orphan, "bet_not_in_coupon")
    smuggled = review_model_output(
        _raw([_row(1, "reject"), _row(2, "bet")], [[1]]),
        _playable(1, 2),
        set(),
        min_odd=MIN,
        max_legs=3,
    )
    _assert_no_bet(smuggled, "not_a_bet")


class _Models:
    def __init__(self, behavior: object) -> None:
        self.behavior = behavior
        self.calls = 0
        self.prompts: list[str] = []
        self.configs: list[object] = []

    async def generate_content(
        self, *, model: str, contents: str, config: object
    ) -> object:
        self.calls += 1
        self.prompts.append(contents)
        self.configs.append(config)
        behavior = self.behavior
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class _Client:
    def __init__(self, models: _Models) -> None:
        self.aio = SimpleNamespace(models=models)


def _provider(behavior: object) -> tuple[GeminiProvider, _Models]:
    models = _Models(behavior)
    provider = GeminiProvider(
        api_key="secret-key", model="gemini-2.5-flash", client=_Client(models)
    )
    return provider, models


def _ok() -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_token_count=11,
        candidates_token_count=7,
        thoughts_token_count=5,
    )
    return SimpleNamespace(
        text='{"candidates":[]}',
        usage_metadata=usage,
        model_version="gemini-2.5-flash-001",
    )


@pytest.mark.asyncio
async def test_gemini_calls_once_and_keeps_the_key_out_of_the_prompt() -> None:
    provider, models = _provider(_ok())
    completion = await provider.decide("score is 0-1", timeout=1)
    assert models.calls == 1
    assert models.prompts == ["score is 0-1"]
    assert "secret-key" not in models.prompts[0]
    assert completion.provider == "google"
    assert completion.model == "gemini-2.5-flash"
    assert completion.model_version == "gemini-2.5-flash-001"
    assert completion.input_tokens == 11
    assert completion.output_tokens == 7
    assert completion.thoughts_tokens == 5
    assert completion.billed_output_tokens == 12
    retry = models.configs[0].http_options.retry_options  # type: ignore[attr-defined]
    assert retry.attempts == 5


@pytest.mark.asyncio
async def test_missing_model_version_falls_back_to_the_requested_model() -> None:
    usage = SimpleNamespace(prompt_token_count=1, candidates_token_count=1)
    reply = SimpleNamespace(text="{}", usage_metadata=usage, model_version="  ")
    provider, _models = _provider(reply)
    completion = await provider.decide("prompt", timeout=1)
    assert completion.model == "gemini-2.5-flash"
    assert completion.model_version == "gemini-2.5-flash"
    assert completion.thoughts_tokens == 0
    assert completion.billed_output_tokens == 1


@pytest.mark.asyncio
async def test_gemini_timeout_network_and_status_do_not_retry() -> None:
    slow_models = _Models(_ok())

    async def hang(**_kwargs: object) -> object:
        slow_models.calls += 1
        await asyncio.sleep(5)
        return _ok()

    slow_models.generate_content = hang  # type: ignore[method-assign]
    provider = GeminiProvider(
        api_key="secret-key",
        model="gemini-2.5-flash",
        client=_Client(slow_models),
    )
    with pytest.raises(ProviderError) as timeout:
        await provider.decide("prompt", timeout=0.01)
    assert timeout.value.code == "timeout"
    assert slow_models.calls == 1

    network, network_models = _provider(httpx.ConnectError("down"))
    with pytest.raises(ProviderError) as down:
        await network.decide("prompt", timeout=1)
    assert down.value.code == "network"
    assert network_models.calls == 1

    expired, expired_models = _provider(_Status(408))
    with pytest.raises(ProviderError) as http_timeout:
        await expired.decide("prompt", timeout=1)
    assert http_timeout.value.code == "timeout"
    assert expired_models.calls == 1

    limited, limited_models = _provider(_Status(429))
    with pytest.raises(ProviderError) as rate:
        await limited.decide("prompt", timeout=1)
    assert rate.value.code == "rate_limit"
    assert limited_models.calls == 1

    server, server_models = _provider(_Status(503))
    with pytest.raises(ProviderError) as failed:
        await server.decide("prompt", timeout=1)
    assert failed.value.code == "server"
    assert server_models.calls == 1

    rejected, rejected_models = _provider(_Status(400))
    with pytest.raises(_Status):
        await rejected.decide("prompt", timeout=1)
    assert rejected_models.calls == 1


class _Status(Exception):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(str(code))
