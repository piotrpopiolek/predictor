"""HTTP client errors. Messages must never include the API key or DSN."""

from __future__ import annotations


class WriterLockRequiredError(RuntimeError):
    """Raised when any API-Football request is attempted without the writer lock."""


class AuthBlockedError(RuntimeError):
    """401/403 from API-Football: new requests must stop (FR-020 / FR-030)."""


class FootballHttpError(RuntimeError):
    def __init__(self, status_code: int, path: str) -> None:
        self.status_code = status_code
        self.path = path
        super().__init__(f"API HTTP {status_code} for {path}")


class RetryableHttpError(RuntimeError):
    def __init__(
        self, status_code: int | None, *, retry_after: float | None = None
    ) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        label = f"HTTP {status_code}" if status_code is not None else "transport error"
        super().__init__(f"retryable {label}")


class QuotaExhaustedError(RuntimeError):
    """Stop paging when remaining quota hits zero; do not mark the task complete."""
