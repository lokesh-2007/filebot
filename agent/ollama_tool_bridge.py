"""Adapt Qwen's plain-text function sketches into Nanobot tool-call records."""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)


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


def _available_tool_names(tools: Any) -> set[str]:
    names: set[str] = set()
    if not isinstance(tools, list):
        return names
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function", tool)
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.add(function["name"])
    return names


def bridge_tool_response(response: Any, tools: Any, call_type: Any) -> Any:
    """Convert one eligible text sketch into the provider's structured response."""
    if getattr(response, "tool_calls", None):
        return response
    parsed = parse_qwen_tool_text(
        getattr(response, "content", None), _available_tool_names(tools),
    )
    if parsed is None:
        return response
    name, arguments = parsed
    response.content = None
    response.tool_calls = [call_type(
        id=f"filebot-{uuid4().hex}", name=name, arguments=arguments,
    )]
    response.finish_reason = "tool_calls"
    log.debug("Converted Qwen text tool sketch into a structured %s call", name)
    return response


def parse_qwen_tool_text(content: str | None, available_tools: set[str]) -> tuple[str, dict[str, Any]] | None:
    """Parse only a complete JSON tool sketch naming a currently offered tool."""
    if not content:
        return None
    try:
        payload = json.loads(_strip_code_fence(content))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    arguments = payload.get("arguments")
    if name not in available_tools or not isinstance(arguments, dict):
        return None
    return name, _normalize_arguments(arguments)


def install_ollama_tool_bridge(provider: Any) -> bool:
    """Translate Qwen plain-text calls before Nanobot processes the response.

    Qwen2.5-Coder 3B on Ollama can return the requested function as JSON text
    instead of populating the OpenAI-compatible ``tool_calls`` field. Nanobot
    only executes structured calls, so this provider-local adapter converts an
    exact, currently offered tool name into the equivalent native record.
    """
    if getattr(provider, "_filebot_tool_bridge_installed", False):
        return False

    # Nanobot's agent runner calls chat_stream_with_retry(), which assembles
    # streamed content into an LLMResponse. Patching chat() alone misses the
    # real desktop path entirely.
    method_name = "chat_stream_with_retry" if hasattr(provider, "chat_stream_with_retry") else "chat"
    original_chat = getattr(provider, method_name)
    from nanobot.providers.base import ToolCallRequest

    async def chat_with_bridge(*args: Any, **kwargs: Any) -> Any:
        response = await original_chat(*args, **kwargs)
        tools = kwargs.get("tools")
        if tools is None and len(args) > 1:
            # Provider methods accept (messages, tools, model, ...).
            tools = args[1]
        return bridge_tool_response(response, tools, ToolCallRequest)

    setattr(provider, method_name, chat_with_bridge)
    provider._filebot_tool_bridge_installed = True
    return True
