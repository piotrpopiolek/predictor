"""Provider contract. The Google SDK stays in ``gemini.py``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


class ProviderError(Exception):
    """The call failed. The cycle places no bet and does not retry."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LlmCompletion:
    text: str
    provider: str
    model: str
    model_version: str
    input_tokens: int
    output_tokens: int
    thoughts_tokens: int

    @property
    def billed_output_tokens(self) -> int:
        """Candidate tokens plus thinking tokens. Both are priced as output."""
        return self.output_tokens + self.thoughts_tokens


class LlmProvider(Protocol):
    provider_name: str
    model: str

    async def decide(self, prompt: str, *, timeout: float) -> LlmCompletion:
        """One completion. No retries."""


def provider_failure_code(exc: Exception) -> str | None:
    """Timeout, network, 408, 429, and 5xx. Other errors are not relabelled."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        return "network"
    code = getattr(exc, "code", None)
    if code == 408:
        return "timeout"
    if code == 429:
        return "rate_limit"
    if isinstance(code, int) and code >= 500:
        return "server"
    return None
