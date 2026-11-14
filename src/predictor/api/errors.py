"""HTTP error mapping. The client sees a stable code; the log keeps the cause."""

from __future__ import annotations

import logging

from fastapi import HTTPException

from predictor.logutil import log_json


def postgres_unavailable(exc: BaseException) -> HTTPException:
    """Log the failure, then answer 503. The body stays free of the driver text."""
    message = _public_cause(exc)
    log_json(
        logging.ERROR,
        service="status",
        event="postgres_unavailable",
        error=type(exc).__name__,
        message=message,
    )
    return HTTPException(status_code=503, detail="postgres_unavailable")


def _public_cause(exc: BaseException) -> str:
    text = str(exc).splitlines()[0].strip() if str(exc) else ""
    folded = text.casefold()
    if "://" in text or "password" in folded or "postgres" in folded:
        return type(exc).__name__
    return text[:160]
