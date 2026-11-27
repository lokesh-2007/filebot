"""Tests for converting Qwen's text-based tool sketches safely."""
from __future__ import annotations

import unittest

from agent.tool_text_bridge import parse_qwen_tool_text


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

    def test_accepts_qwen_tool_call_wrapper(self) -> None:
        content = '<tool_call>\n{"name":"search_files","arguments":{"query":"notes"}}\n</tool_call>'
        self.assertEqual(parse_qwen_tool_text(content, {"search_files"}),
                         ("search_files", {"query": "notes"}))


if __name__ == "__main__":
    unittest.main()
