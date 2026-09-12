"""JSON-line logging without secrets (FR-037)."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from predictor.schemas.settings import Settings, startup_log_fields


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def log_json(level: int, **fields: Any) -> None:
    logging.log(level, json.dumps(fields, default=str, ensure_ascii=True))


def log_startup(settings: Settings, *, service: str) -> None:
    payload = {"service": service, "event": "startup"}
    payload.update(startup_log_fields(settings))
    log_json(logging.INFO, **payload)
