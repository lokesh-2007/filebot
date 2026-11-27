"""Cheap intent router that runs before any language model call."""
from __future__ import annotations

import re


class FastRouter:
    """Route obvious file actions and command requests without extra model work."""
    _summary_request = re.compile(r"\bsummari[sz]e\b", re.I)
    _confirm_delete = re.compile(r"\bconfirm\s+delete\s+([a-f0-9]{8})\b", re.I)
    _cancel_delete = re.compile(r"\bcancel\s+delete\s+([a-f0-9]{8})\b", re.I)
    _cmd = re.compile(
        r"\b(?:use\s+cmd|command prompt|terminal command)\b"
        r"|\b(?:run|execute)\s+(?:the\s+|this\s+|a\s+)?"
        r"(?:command|cmd|script|program|tests?|build|task|file)\b"
        r"|\b(?:run|execute)\s+(?:`[^`]+`|'[^']+'|\"[^\"]+\")"
        r"|\brun\s+(?:whoami|dir|ipconfig|npm|python|git|cargo|pytest|powershell)\b",
        re.I,
    )
    _search = re.compile(r"\b(find|search|locate|where is|look for)\b", re.I)
    _powershell_request = re.compile(
        r"\b(?:use|run|execute)\s+(?:windows\s+)?(?:powershell|pwsh)\b"
        r"|^\s*(?:powershell|pwsh)\s+(?:-command\s+)?"
        r"|^\s*(?:Get|New|Copy|Move|Rename|Set|Start|Stop|Test|Select|Where|Measure|Format|Out|Write|Read|Import|Export)-[A-Za-z][\w-]*\b",
        re.I,
    )
    _delete_file = re.compile(r"\b(?:delete|remove|trash)\b", re.I)
    _create_file = re.compile(
        r"^\s*(?:(?:please|i need you to|i need to|i want you to|i want to|"
        r"can you|could you|would you)\s+)?(?:create|make|write|save)\b"
        r".{0,80}\b(?:new\s+)?file\b"
        r"|^\s*(?:please\s+)?(?:create|make)\s+(?:a\s+)?(?:new\s+)?"
        r"[\w ._-]+\.(?:txt|md|csv|json|ya?ml|py|js|ts|rs|html)\b",
        re.I,
    )
    _action_question = re.compile(
        r"^\s*(?:how|why|when|what|should\s+i|can\s+i)\b.*"
        r"\b(?:delete|remove|trash|create|make|write|save|open|browse)\b",
        re.I,
    )
    _open_file = re.compile(
        r"\b(?:open|view|show)\b.{0,48}\b(?:file|document|pdf|docx|notes?)\b"
        r"|\b(?:open|view)\s+[\"']?[\w./\\:-]+\.(?:txt|md|pdf|docx)\b",
        re.I,
    )
    _open_folder = re.compile(
        r"\b(?:open|show|view|browse|go\s+to|navigate\s+to)\b"
        r".{0,100}\b(?:folder|directory)\b",
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
    _delete_target = re.compile(
        r"\b(?:delete|remove|trash)\s+(?:(?:the|this|my)\s+)?(?:file\s+)?"
        r"(?:(?:named|called)\s+)?[\"']?([^\"'<>|?*\r\n]+?\.[a-z0-9]{1,10})"
        r"[\"']?(?=$|\s|[,!?])",
        re.I,
    )

    def confirmation_token(self, prompt: str) -> str | None:
        match = self._confirm_delete.search(prompt.strip())
        return match.group(1).upper() if match else None

    def cancellation_token(self, prompt: str) -> str | None:
        match = self._cancel_delete.search(prompt.strip())
        return match.group(1).upper() if match else None

    def delete_target(self, prompt: str) -> str | None:
        match = self._delete_target.search(prompt.strip())
        return match.group(1).strip().strip("`").rstrip(".,;") if match else None

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
        target = re.sub(r"\s+(?:folder|directory)$", "", target, flags=re.I)
        return target or None

    def folder_target(self, prompt: str) -> str | None:
        """Extract an explicit folder name/path from a direct open-folder request."""
        inside = re.search(
            r"\b(?:open|show|view|browse|go\s+to|navigate\s+to)\s+"
            r"(?:(?:me|the|my)\s+)*(?P<path>.+?)\s+(?:folder|directory)\s+"
            r"(?:inside|in)\s+(?:my\s+)?(?:onedrive|documents|downloads|desktop)\s*[?.!]*$",
            prompt.strip(), re.I,
        )
        if inside is not None:
            target = inside.group("path").strip().strip('"').strip("'")
            target = re.sub(r"^(?:the|my)\s+", "", target, flags=re.I)
            return target or None
        match = re.search(
            r"\b(?:open|show|view|browse|go\s+to|navigate\s+to)\s+"
            r"(?:(?:me|the|my)\s+)*(?P<path>.+?)\s+(?:folder|directory)\s*[?.!]*$",
            prompt.strip(), re.I,
        )
        if match is None:
            return None
        target = match.group("path").strip().strip('"').strip("'")
        target = re.sub(r"^(?:the|my)\s+", "", target, flags=re.I)
        return target or None

    def is_summary_request(self, prompt: str) -> bool:
        """Keep summary requests model-backed even when the named file is local."""
        return bool(self._summary_request.search(prompt))

    def route(self, prompt: str) -> str:
        """Route prompt intent; CMD and delete remain behind explicit confirmations."""
        if self._powershell_request.search(prompt):
            return "powershell"
        if self._cmd.search(prompt):
            return "cmd"
        if self._confirm_delete.search(prompt):
            return "confirm_delete"
        if self._cancel_delete.search(prompt):
            return "cancel_delete"
        if self._delete_file.search(prompt) and not self._action_question.search(prompt):
            return "delete"
        if self._create_file.search(prompt) and not self._action_question.search(prompt):
            return "create"
        if self._open_folder.search(prompt) and not self._action_question.search(prompt):
            return "open_folder"
        if self._search.search(prompt):
            return "search"
        if self._read_file.search(prompt):
            return "read"
        if self._open_file.search(prompt):
            return "open"
        if self._list_folder.search(prompt):
            return "list"
        return "chat"
