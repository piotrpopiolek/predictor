"""Pure cases for this layer. They do not open Postgres."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from predictor.schemas.advisor import AdvisorSettings
from predictor.services.advisor.langfuse import (
    EU_HOST,
    LangfuseSink,
    TraceDraft,
    TraceExportError,
    build_trace,
    redact,
    sink_from_settings,
)

NOW = datetime(2026, 10, 18, 19, 0, tzinfo=UTC)

SECRET = "llm-key-do-not-leak"

LEAK = (
    f"use {SECRET} AIzaSyDUMMYKEY0123456789abcd "
    "postgresql+psycopg://predictor_app:s3cret@db.internal:5432/predictor_prod "
    "postgres://user:pass@db.internal/predictor "
    "Bearer eyJhbGciOi.part+tailsecret"
)


def _settings() -> AdvisorSettings:
    return AdvisorSettings(
        llm_api_key="secret",
        llm_price_input_pln_per_mtok=Decimal("0.50"),
        llm_price_output_pln_per_mtok=Decimal("1.25"),
    )


_PLAIN = TraceDraft(
    prompt_version="p1",
    prompt="score",
    model_input={"note": "plain"},
    output="{}",
    provider="google",
    model="gemini-2.5-flash",
    model_version="gemini-2.5-flash-001",
    mode="sandbox",
    status="ok",
    input_tokens=12,
    output_tokens=4,
    thoughts_tokens=1,
    cost_pln=Decimal("0.0100"),
    latency_ms=8,
    started_at=NOW,
    finished_at=NOW,
)


def test_redact_strips_keys_dsns_and_bearer_tokens() -> None:
    cleaned = redact(
        {"note": LEAK, "nested": [LEAK]},
        (SECRET,),
    )
    rendered = json.dumps(cleaned)
    assert SECRET not in rendered
    assert "AIzaSy" not in rendered
    assert "postgres://" not in rendered
    assert "postgresql+psycopg" not in rendered
    assert "db.internal" not in rendered
    assert "predictor_prod" not in rendered
    assert "s3cret" not in rendered
    assert "tailsecret" not in rendered
    assert "[redacted]" in rendered


def test_provider_error_is_an_error_span() -> None:
    export = build_trace(
        replace(_PLAIN, status="provider_error", prompt=LEAK),
        (SECRET,),
    )
    rendered = json.dumps(export.body)
    assert SECRET not in rendered
    assert "db.internal" not in rendered
    assert "tailsecret" not in rendered
    spans = export.body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    decide = next(item for item in spans if item["name"] == "decide")
    assert decide["traceId"] == export.trace_id
    assert decide["status"]["code"] == 2
    assert decide["status"]["message"] == "provider_error"
    assert len(export.trace_id) == 32


def test_trace_children_follow_the_graph_and_only_decide_is_a_generation() -> None:
    export = build_trace(replace(_PLAIN, exit_node="interrupt"), ())
    spans = export.body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    names = [item["name"] for item in spans]
    assert names == ["advisor.cycle", "prepare", "decide", "validate", "interrupt"]
    parent = spans[0]["spanId"]
    assert "parentSpanId" not in spans[0]
    assert all(item["parentSpanId"] == parent for item in spans[1:])
    kinds = {item["name"]: _attr(item, "langfuse.observation.type") for item in spans}
    assert kinds["decide"] == "generation"
    assert kinds["prepare"] is None
    assert kinds["validate"] is None
    assert kinds["interrupt"] is None
    decide = next(item for item in spans if item["name"] == "decide")
    assert _attr(spans[1], "gen_ai.usage.input_tokens") is None
    assert _attr(decide, "gen_ai.request.model") == "gemini-2.5-flash"


def _attr(span: dict, key: str) -> str | None:
    for item in span["attributes"]:
        if item["key"] == key:
            value = item["value"]
            return str(value.get("stringValue", value.get("intValue")))
    return None


def test_empty_langfuse_keys_skip_the_export() -> None:
    settings = _settings()
    assert settings.langfuse_host == EU_HOST
    assert sink_from_settings(settings) is None
    with pytest.raises(ValidationError):
        AdvisorSettings(
            llm_api_key="secret",
            llm_price_input_pln_per_mtok=Decimal("1"),
            llm_price_output_pln_per_mtok=Decimal("1"),
            langfuse_host="http://cloud.langfuse.com",
        )
    with pytest.raises(ValidationError):
        AdvisorSettings(
            llm_api_key="secret",
            llm_price_input_pln_per_mtok=Decimal("1"),
            llm_price_output_pln_per_mtok=Decimal("1"),
            langfuse_host="https://cloud.langfuse.com/api/public/otel",
        )
    trimmed = AdvisorSettings(
        llm_api_key="secret",
        llm_price_input_pln_per_mtok=Decimal("1"),
        llm_price_output_pln_per_mtok=Decimal("1"),
        langfuse_host="https://cloud.langfuse.com/",
    )
    assert trimmed.langfuse_host == EU_HOST


@pytest.mark.asyncio
async def test_sink_posts_otlp_to_the_eu_endpoint() -> None:
    seen: dict[str, str] = {}

    def ok(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["version"] = request.headers["x-langfuse-ingestion-version"]
        raw = request.headers["authorization"].split(" ", 1)[1]
        seen["auth"] = base64.b64decode(raw).decode()
        return httpx.Response(200)

    sink = LangfuseSink(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        transport=httpx.MockTransport(ok),
    )
    await sink.send({"resourceSpans": []})
    assert seen["url"] == f"{EU_HOST}/api/public/otel/v1/traces"
    assert seen["version"] == "4"
    assert seen["auth"] == "pk-lf-test:sk-lf-test"

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    failing = LangfuseSink(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        transport=httpx.MockTransport(down),
    )
    with pytest.raises(TraceExportError, match="http_503"):
        await failing.send({})
