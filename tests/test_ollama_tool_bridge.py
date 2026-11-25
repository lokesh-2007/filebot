"""Tests for converting Qwen's text-based tool sketches safely."""
from __future__ import annotations

import unittest

from agent.ollama_tool_bridge import bridge_tool_response, parse_qwen_tool_text


class QwenToolTextTests(unittest.TestCase):
    def test_parses_only_a_currently_available_tool_and_unwraps_arguments(self) -> None:
        content = '''```json
{"name":"open_file","arguments":{"path":{"type":"string","value":"C:/docs/notes.md"}}}
```'''
        result = parse_qwen_tool_text(content, {"open_file"})
        self.assertEqual(result, ("open_file", {"path": "C:/docs/notes.md"}))

    def test_rejects_unknown_tools_malformed_json_and_non_object_arguments(self) -> None:
        self.assertIsNone(parse_qwen_tool_text(
            '{"name":"exec","arguments":{"command":"anything"}}', {"open_file"}
        ))
        self.assertIsNone(parse_qwen_tool_text('{broken json}', {"open_file"}))
        self.assertIsNone(parse_qwen_tool_text(
            '{"name":"open_file","arguments":["path"]}', {"open_file"}
        ))
        self.assertIsNone(parse_qwen_tool_text("The answer is 42.", {"open_file"}))

    def test_provider_response_bridge_accepts_positional_tool_definitions(self) -> None:
        class FakeCall:
            def __init__(self, *, id, name, arguments):
                self.id, self.name, self.arguments = id, name, arguments

        class FakeResponse:
            content = '{"name":"search_files","arguments":{"query":{"type":"string","value":"notes"}}}'
            tool_calls = []
            finish_reason = "stop"

        tools = [{"type": "function", "function": {"name": "search_files"}}]
        response = bridge_tool_response(FakeResponse(), tools, FakeCall)
        self.assertIsNone(response.content)
        self.assertEqual(response.finish_reason, "tool_calls")
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "search_files")
        self.assertEqual(response.tool_calls[0].arguments, {"query": "notes"})


if __name__ == "__main__":
    unittest.main()
