"""Cheap intent router that runs before any language model call."""
from __future__ import annotations

import re


class FastRouter:
    """Route obvious file actions and command requests without extra model work."""
    _cmd = re.compile(
        r"\b(?:use\s+cmd|command prompt|terminal command)\b"
        r"|\b(?:run|execute)\s+(?:the\s+|this\s+|a\s+)?"
        r"(?:command|cmd|script|program|tests?|build|task|file)\b"
        r"|\b(?:run|execute)\s+(?:`[^`]+`|'[^']+'|\"[^\"]+\")"
        r"|\brun\s+(?:whoami|dir|ipconfig|npm|python|git|cargo|pytest|powershell)\b",
        re.I,
    )
    _search = re.compile(r"\b(find|search|locate|where is|look for)\b", re.I)
    _open_file = re.compile(
        r"\b(?:open|view|show)\b.{0,48}\b(?:file|document|pdf|docx|folder|notes?)\b"
        r"|\b(?:open|view)\s+[\"']?[\w./\\:-]+\.(?:txt|md|pdf|docx)\b",
        re.I,
    )
    _read_file = re.compile(r"\b(?:read|summari[sz]e)\b", re.I)
    _open_target = re.compile(
        r"\b(?:open|view|show|read)\s+(?:me\s+)?(?:the\s+)?(?:file\s+)?[\"']?"
        r"([^\"'<>|?*\r\n]+?\.[a-z0-9]{1,10})[\"']?(?=$|\s|[,!?])",
        re.I,
    )

    def open_target(self, prompt: str) -> str | None:
        """Extract an explicitly named file for safe, model-free read or open."""
        match = self._open_target.search(prompt.strip())
        if match is None:
            return None
        return match.group(1).strip().rstrip(".,;") or None

    def route(self, prompt: str) -> str:
        """Return cmd, search, open, or chat; CMD intent only proposes for approval."""
        if self._cmd.search(prompt):
            return "cmd"
        if self._search.search(prompt):
            return "search"
        if self._read_file.search(prompt):
            return "read"
        if self._open_file.search(prompt):
            return "open"
        return "chat"
