"""Run the frozen stability set twice. Requires LLM_API_KEY. Not a CI test."""

from __future__ import annotations

import asyncio
import sys

from pydantic import ValidationError

from predictor.schemas.advisor import AdvisorSettings, LlmDecision
from predictor.services.advisor.llm.gemini import GeminiProvider
from predictor.services.advisor.llm.protocol import ProviderError
from predictor.services.advisor.llm.schema import response_json_schema
from predictor.services.advisor.stability import load_cases, score_replies


async def _run() -> int:
    try:
        settings = AdvisorSettings()
    except ValidationError:
        print("LLM_API_KEY and token prices are required", file=sys.stderr)
        return 2
    provider = GeminiProvider(
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model,
    )
    schema = response_json_schema(LlmDecision)
    cases = load_cases()
    replies: list[tuple[str | None, str | None]] = []
    for case in cases:
        pair: list[str | None] = []
        for _pass in range(2):
            try:
                completion = await provider.decide(
                    case.prompt,
                    timeout=float(settings.llm_timeout_seconds),
                    response_schema=schema,
                )
            except ProviderError:
                pair.append(None)
                continue
            pair.append(completion.text)
        replies.append((pair[0], pair[1]))
    print(score_replies(cases, tuple(replies)).render(), end="")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
