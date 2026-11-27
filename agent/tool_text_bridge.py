"""Adapt small models' plain-text function sketches into structured tool calls."""
from __future__ import annotations

import json
from typing import Any


def _strip_code_fence(content: str) -> str:
    value = content.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1]
        if value.endswith("```"):
            value = value[:-3].strip()
    return value


def _normalize_arguments(value: Any) -> Any:
    """Unwrap Qwen's JSON-schema-shaped {type, value} argument encoding."""
    if isinstance(value, dict):
        if set(value) == {"type", "value"} and value.get("type") in {
            "string", "integer", "number", "boolean", "object", "array", "null",
        }:
            return _normalize_arguments(value["value"])
        return {key: _normalize_arguments(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_arguments(item) for item in value]
    return value


def parse_qwen_tool_text(content: str | None, available_tools: set[str]) -> tuple[str, dict[str, Any]] | None:
    """Parse only a complete JSON tool sketch naming a currently offered tool.

    Also accepts Qwen's ``<tool_call>{...}</tool_call>`` wrapper.
    """
    if not content:
        return None
    value = _strip_code_fence(content)
    if value.startswith("<tool_call>"):
        value = value[len("<tool_call>"):]
        if value.rstrip().endswith("</tool_call>"):
            value = value.rstrip()[:-len("</tool_call>")]
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    arguments = payload.get("arguments")
    if name not in available_tools or not isinstance(arguments, dict):
        return None
    return name, _normalize_arguments(arguments)
