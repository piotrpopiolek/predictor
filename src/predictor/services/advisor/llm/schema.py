"""JSON Schema sent with a model call. Built from the Pydantic contract."""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel

_NUMBER_BRANCHES = frozenset({"number", "integer", "string"})


def response_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Schema for ``response_json_schema``. Decimals are numbers, not strings."""
    raw = model.model_json_schema()
    defs = dict(raw.get("$defs") or {})
    body = {key: value for key, value in raw.items() if key != "$defs"}
    return cast(dict[str, Any], _tighten(_inline(body, defs)))


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_inline(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = ref.removeprefix("#/$defs/")
        target = _inline(defs[name], defs)
        extras = {
            key: _inline(value, defs) for key, value in node.items() if key != "$ref"
        }
        if isinstance(target, dict):
            return {**target, **extras}
        return target
    return {key: _inline(value, defs) for key, value in node.items()}


def _tighten(node: Any) -> Any:
    """Drop the string branch Pydantic adds for Decimal, and fold const unions."""
    if isinstance(node, list):
        return [_tighten(item) for item in node]
    if not isinstance(node, dict):
        return node
    node = {key: _tighten(value) for key, value in node.items()}
    options = node.get("anyOf")
    if not isinstance(options, list) or not options:
        return node
    if all(isinstance(item, dict) and "const" in item for item in options):
        rest = {key: value for key, value in node.items() if key != "anyOf"}
        rest["type"] = "string"
        rest["enum"] = [item["const"] for item in options]
        return rest
    types = [
        item.get("type")
        for item in options
        if isinstance(item, dict) and item.get("type") in _NUMBER_BRANCHES
    ]
    if "number" in types and set(types) <= _NUMBER_BRANCHES:
        number = next(item for item in options if item.get("type") == "number")
        rest = {key: value for key, value in node.items() if key != "anyOf"}
        return {**number, **rest, "type": "number"}
    return node
