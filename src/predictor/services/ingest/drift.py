"""FR-006: unknown API fields are logged, not dropped silently."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from predictor.logutil import log_json


def log_contract_drift(endpoint: str, extras: dict[str, Any] | None) -> None:
    if not extras:
        return
    log_json(
        logging.WARNING,
        service="worker",
        event="contract_drift",
        endpoint=endpoint,
        fields=sorted(extras.keys()),
    )


def warn_model_extra(endpoint: str, model: BaseModel) -> None:
    extra = model.model_extra
    if extra:
        log_contract_drift(endpoint, extra)
