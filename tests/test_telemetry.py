from __future__ import annotations

import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from predictor.logutil import configure_logging, log_json
from predictor.schemas.settings import load_settings
from predictor.telemetry import configure_telemetry, start_span


def test_configure_telemetry_without_endpoint_is_noop(valid_env: None) -> None:
    settings = load_settings()
    assert settings.otel_exporter_otlp_endpoint is None
    assert configure_telemetry(settings, service="predictor-worker") is False


def test_empty_otel_endpoint_is_none(
    valid_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "  ")
    settings = load_settings()
    assert settings.otel_exporter_otlp_endpoint is None


def test_log_json_includes_trace_ids_and_omits_secrets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous = trace.get_tracer_provider()
    trace.set_tracer_provider(provider)
    configure_logging()
    try:
        with start_span("etl.transaction", etl_endpoint="/fixtures", api_key="secret"):
            log_json(logging.INFO, service="worker", event="span_test")
        text = capsys.readouterr().out
        spans = exporter.get_finished_spans()
        assert spans
        attrs = dict(spans[0].attributes or {})
        assert "api_key" not in attrs
        assert attrs["etl_endpoint"] == "/fixtures"
        assert "trace_id" in text
        assert "span_id" in text
        assert "secret" not in text
    finally:
        trace.set_tracer_provider(previous)
