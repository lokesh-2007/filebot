"""Cheap intent router that runs before any language model call."""
from __future__ import annotations

import re


class FastRouter:
    """Route obvious file searches and command requests without an LLM."""
    _cmd = re.compile(r"\b(run|execute|use cmd|command prompt|terminal command)\b", re.I)
    _search = re.compile(r"\b(find|search|locate|where is|look for)\b", re.I)

    def route(self, prompt: str) -> str:
        """Return cmd, search, or chat. CMD intent is intercepted for approval."""
        if self._cmd.search(prompt):
            return "cmd"
        if self._search.search(prompt):
            return "search"
        return "chat"
