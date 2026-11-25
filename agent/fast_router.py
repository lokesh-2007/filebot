"""Cheap intent router that runs before any language model call."""
from __future__ import annotations

import re


class FastRouter:
    """Route obvious file searches and command requests without an LLM."""
    _cmd = re.compile(r"\b(run|execute|use cmd|command prompt|terminal command)\b", re.I)
    _search = re.compile(r"\b(find|search|locate|where is|look for)\b", re.I)
    _open_file = re.compile(
        r"\b(?:open|view|show)\b.{0,48}\b(?:file|document|pdf|docx|folder|notes?)\b"
        r"|\b(?:open|view)\s+[\"']?[\w./\\:-]+\.(?:txt|md|pdf|docx)\b",
        re.I,
    )

    def route(self, prompt: str) -> str:
        """Return cmd, search, open, or chat; CMD intent only proposes for approval."""
        if self._cmd.search(prompt):
            return "cmd"
        if self._search.search(prompt):
            return "search"
        if self._open_file.search(prompt):
            return "open"
        return "chat"
