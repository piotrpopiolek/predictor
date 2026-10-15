"""OpenTelemetry setup. Export failures must never stop the writer (FR-046 / §4.3)."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import Span, Status, StatusCode
from sqlalchemy.ext.asyncio import AsyncEngine

from predictor.logutil import log_json
from predictor.schemas.settings import Settings

_configured = False
_drift_counter: Any = None
_SECRET_ATTR_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "api_sports_key",
        "password",
        "dsn",
        "token",
        "x-apisports-key",
        "telegram_bot_token",
        "operator_bet_token",
        "postgres_password",
    }
)
_HEADER_SANITIZE_ENV = "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS"
_SANITIZED_HEADERS = ("x-apisports-key", "authorization")


def _ensure_header_sanitize() -> None:
    """Redact the API key if header capture is turned on via env."""
    current = [
        part.strip()
        for part in os.environ.get(_HEADER_SANITIZE_ENV, "").split(",")
        if part.strip()
    ]
    folded = {part.casefold() for part in current}
    for name in _SANITIZED_HEADERS:
        if name not in folded:
            current.append(name)
    os.environ[_HEADER_SANITIZE_ENV] = ",".join(current)


def scrub_client_span(span: Span, request: Any) -> None:
    """Drop API-key header attributes the httpx instrumentor may have copied."""
    del request
    store = getattr(span, "_attributes", None)
    if not store:
        return
    secret_markers = tuple(name.casefold() for name in _SANITIZED_HEADERS)
    for key in list(store):
        folded = str(key).casefold().replace("_", "-")
        if any(marker in folded for marker in secret_markers):
            try:
                del store[key]
            except TypeError:
                span.set_attribute(key, "[REDACTED]")


async def _scrub_client_span_async(span: Span, request: Any) -> None:
    scrub_client_span(span, request)


def _resource(service: str, settings: Settings) -> Resource:
    return Resource.create(
        {
            "service.name": service,
            "service.namespace": "predictor",
            "deployment.environment": settings.host_environment.value,
            "predictor.instance_id": settings.instance_id,
        }
    )


def configure_telemetry(settings: Settings, *, service: str) -> bool:
    """Install Tracer/Meter providers.

    Returns False if export is off or setup failed.
    """
    global _configured
    endpoint = settings.otel_exporter_otlp_endpoint
    if not endpoint:
        return False
    if _configured:
        return True
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        base = endpoint.rstrip("/")
        resource = _resource(service, settings)
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{base}/v1/traces"),
            )
        )
        trace.set_tracer_provider(tracer_provider)
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{base}/v1/metrics")
        )
        metrics.set_meter_provider(
            MeterProvider(resource=resource, metric_readers=[reader])
        )
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        _ensure_header_sanitize()
        HTTPXClientInstrumentor().instrument(
            request_hook=scrub_client_span,
            async_request_hook=_scrub_client_span_async,
        )
        LoggingInstrumentor().instrument(set_logging_format=False)
        _configured = True
        return True
    except Exception:
        log_json(
            logging.WARNING,
            service=service,
            event="telemetry_setup_failed",
        )
        return False


def instrument_fastapi(app: Any) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
    except Exception:
        log_json(logging.WARNING, service="status", event="telemetry_fastapi_failed")


def instrument_engine(engine: AsyncEngine) -> None:
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    except Exception:
        log_json(logging.WARNING, service="worker", event="telemetry_sqlalchemy_failed")


def record_contract_drift(endpoint: str) -> None:
    global _drift_counter
    try:
        if _drift_counter is None:
            meter = metrics.get_meter("predictor")
            _drift_counter = meter.create_counter(
                "predictor_contract_drift_total",
                description="Unknown API fields (finite endpoint template label).",
            )
        _drift_counter.add(1, {"endpoint": endpoint, "service": "worker"})
    except Exception:
        return


@contextmanager
def start_span(name: str, **attributes: Any) -> Iterator[Span]:
    tracer = trace.get_tracer("predictor")
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is None:
                continue
            if key.casefold() in _SECRET_ATTR_KEYS:
                continue
            if isinstance(value, (str, int, float, bool)):
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            raise
