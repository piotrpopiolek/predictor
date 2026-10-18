"""Strict parse of one model reply. Invalid JSON is not repaired."""

from __future__ import annotations

import json

from pydantic import ValidationError

from predictor.schemas.advisor import LlmDecision


class InvalidDecision(Exception):
    """The reply is not the schema. Callers record ``invalid_output``."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def parse_decision(raw: str) -> LlmDecision:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidDecision("not_json") from exc
    try:
        return LlmDecision.model_validate(payload)
    except ValidationError as exc:
        raise InvalidDecision("schema") from exc
