"""JSON-line logging without secrets (FR-037)."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from opentelemetry import trace

from predictor.schemas.settings import Settings, startup_log_fields


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _trace_fields() -> dict[str, str]:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return {}
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
    }


def log_json(level: int, **fields: Any) -> None:
    payload = {**_trace_fields(), **fields}
    logging.log(level, json.dumps(payload, default=str, ensure_ascii=True))


def log_startup(settings: Settings, *, service: str) -> None:
    payload = {"service": service, "event": "startup"}
    payload.update(startup_log_fields(settings))
    log_json(logging.INFO, **payload)
