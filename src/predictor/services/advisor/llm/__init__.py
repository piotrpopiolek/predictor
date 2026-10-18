"""LLM adapter. Importing this package does not import the Google SDK."""

from predictor.services.advisor.llm.parse import InvalidDecision, parse_decision
from predictor.services.advisor.llm.protocol import (
    LlmCompletion,
    LlmProvider,
    ProviderError,
    provider_failure_code,
)
from predictor.services.advisor.llm.validate import (
    AcceptedCoupon,
    PricedCandidate,
    Verdict,
    review_model_output,
)

__all__ = [
    "AcceptedCoupon",
    "InvalidDecision",
    "LlmCompletion",
    "LlmProvider",
    "PricedCandidate",
    "ProviderError",
    "Verdict",
    "parse_decision",
    "provider_failure_code",
    "review_model_output",
]
