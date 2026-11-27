"""Cheap intent router that runs before any language model call."""
from __future__ import annotations

import re


class FastRouter:
    """Route obvious file actions and command requests without extra model work."""
    _summary_request = re.compile(r"\bsummari[sz]e\b", re.I)
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
    _read_file = re.compile(
        r"\b(?:read|summari[sz]e)\b|\bwhat(?:'s| is)\s+in\b.{0,100}"
        r"\.(?:txt|md|pdf|docx)\b",
        re.I,
    )
    _list_folder = re.compile(
        r"\blist(?:\s+(?:files|contents))?(?:\s+(?:in|of))?\b"
        r"|\bwhat(?:'s| is)\s+in\b|\bwhat\s+files\s+(?:are\s+)?in\b"
        r"|\bcontents\s+of\b"
        r"|\b(?:show|display|browse)\s+(?:me\s+)?(?:the\s+)?"
        r"(?:files|contents|items)\s+(?:in|of)\b",
        re.I,
    )
    _open_target = re.compile(
        r"\b(?:(?:open|view|show|read|summari[sz]e)\s+(?:me\s+)?(?:the\s+)?"
        r"(?:(?:attached|this|that|my)\s+)?(?:file\s+)?|what(?:'s| is)\s+in\s+)"
        r"[\"']?([^\"'<>|?*\r\n]+?\.[a-z0-9]{1,10})[\"']?(?=$|\s|[,!?])",
        re.I,
    )
    _list_target = re.compile(
        r"\b(?:list(?:\s+(?:files|contents))?(?:\s+(?:in|of))?|"
        r"what(?:'s| is)\s+in|what\s+files\s+(?:are\s+)?in|contents\s+of|"
        r"(?:show|display|browse)\s+(?:me\s+)?(?:the\s+)?"
        r"(?:files|contents|items)\s+(?:in|of))\s*(?:the\s+|my\s+)?"
        r"(?P<path>[^?!]+?)\s*[?.!]*$",
        re.I,
    )

    def open_target(self, prompt: str) -> str | None:
        """Extract an explicitly named file for safe, model-free read or open."""
        match = self._open_target.search(prompt.strip())
        if match is None:
            return None
        return match.group(1).strip().rstrip(".,;") or None

    def list_target(self, prompt: str) -> str | None:
        """Extract an optional folder name from a natural-language listing request."""
        match = self._list_target.search(prompt.strip())
        if match is None:
            return None
        target = match.group("path").strip().strip('"').strip("'").rstrip(".,;?!")
        target = re.sub(r"^(?:files|contents)\s+(?:in|of)\s+", "", target, flags=re.I)
        return target or None

    def is_summary_request(self, prompt: str) -> bool:
        """Keep summary requests model-backed even when the named file is local."""
        return bool(self._summary_request.search(prompt))

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
        if self._list_folder.search(prompt):
            return "list"
        return "chat"
