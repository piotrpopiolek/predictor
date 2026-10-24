"""Google Gemini. The only module that imports the vendor SDK."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from predictor.services.advisor.llm.protocol import (
    LlmCompletion,
    ProviderError,
    provider_failure_code,
)

_PROVIDER = "google"
# Includes the first call. The SDK retries 408, 429 and 5xx inside this one request.
_ATTEMPTS = 5


class GeminiProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: Any | None = None,
    ) -> None:
        self.provider_name = _PROVIDER
        self.model = model
        self._api_key = api_key
        self._client = client

    async def decide(
        self,
        prompt: str,
        *,
        timeout: float,
        response_schema: Mapping[str, Any],
    ) -> LlmCompletion:
        try:
            response = await asyncio.wait_for(
                self._generate(prompt, timeout, response_schema),
                timeout,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise ProviderError("timeout") from exc
        except Exception as exc:
            code = provider_failure_code(exc)
            if code is None:
                raise
            raise ProviderError(code) from exc
        usage = getattr(response, "usage_metadata", None)
        text = getattr(response, "text", None) or ""
        return LlmCompletion(
            text=text,
            provider=self.provider_name,
            model=self.model,
            model_version=_model_version(response, self.model),
            input_tokens=_tokens(usage, "prompt_token_count"),
            output_tokens=_tokens(usage, "candidates_token_count"),
            thoughts_tokens=_tokens(usage, "thoughts_token_count"),
        )

    async def _generate(
        self,
        prompt: str,
        timeout: float,
        response_schema: Mapping[str, Any],
    ) -> Any:
        from google.genai import types

        client = self._client if self._client is not None else self._new_client()
        return await client.aio.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=dict(response_schema),
                temperature=0,
                http_options=types.HttpOptions(
                    timeout=int(timeout * 1000),
                    retry_options=types.HttpRetryOptions(attempts=_ATTEMPTS),
                ),
            ),
        )

    def _new_client(self) -> Any:
        from google import genai

        client = genai.Client(api_key=self._api_key)
        self._client = client
        return client


def _model_version(response: Any, requested: str) -> str:
    snapshot = getattr(response, "model_version", None)
    if isinstance(snapshot, str) and snapshot.strip():
        return snapshot.strip()
    return requested


def _tokens(usage: Any, name: str) -> int:
    if usage is None:
        return 0
    value = getattr(usage, name, None)
    if value is None:
        return 0
    return int(value)
