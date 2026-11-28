"""Cheap intent router that runs before any language model call."""
from __future__ import annotations

import re
from difflib import SequenceMatcher


class FastRouter:
    """Route obvious file actions and command requests without extra model work."""
    _action_words = (
        "browse", "called", "cmd", "command", "contents", "create", "delete",
        "directory", "execute", "extract", "file", "find", "folder", "generate",
        "list", "locate", "make", "named", "open", "powershell", "prompt",
        "pwsh", "read", "remove", "run", "save", "search", "show", "summarize",
        "trash", "view", "write",
    )
    _cmd_prompt_open = re.compile(
        r"^\s*(?:(?:please|kindly|can you|could you|would you|"
        r"i want you to|i need you to)\s+)?(?:open|launch|start|show)\s+"
        r"(?:(?:the|windows)\s+)*(?:cmd(?:\.exe)?(?:\s+prompt)?|command\s+prompt|terminal)"
        r"\s*[?.!]*$",
        re.I,
    )
    _cmd_help = re.compile(r"^\s*(?:cmd(?:\.exe)?|command\s+prompt|terminal)\s*[?.!]*$", re.I)
    _cmd_prefix = re.compile(
        r"^\s*(?:(?:please|kindly|can you|could you|would you)\s+)?"
        r"(?:cmd(?:\.exe)?|command(?:\s+prompt)?|terminal)\s*[:>]\s*(?P<command>.+?)\s*$",
        re.I,
    )
    _bare_cmd = re.compile(
        r"^\s*(?:dir|cd|tree|type|where|whoami|systeminfo|ipconfig|tasklist|"
        r"ver|help|echo|cls|mkdir|copy|move|ren|rename|set|assoc|ftype|path|prompt)\b",
        re.I,
    )
    _powershell_prefix = re.compile(
        r"^\s*(?:(?:please|kindly|can you|could you|would you)\s+)?"
        r"(?:powershell|pwsh)\s*[:>]\s*(?P<command>.+?)\s*$",
        re.I,
    )
    _summary_request = re.compile(r"\bsummari[sz]e\b", re.I)
    _confirm_delete = re.compile(r"\bconfirm\s+delete\s+([a-f0-9]{8})\b", re.I)
    _cancel_delete = re.compile(r"\bcancel\s+delete\s+([a-f0-9]{8})\b", re.I)
    _cmd = re.compile(
        r"\b(?:use\s+cmd|command\s+prompt|cmd\s+prompt|terminal\s+command)\b"
        r"|^\s*(?:cmd(?:\.exe)?|command(?:\s+prompt)?|terminal)\s*[:>]\s*\S"
        r"|^\s*cmd(?:\.exe)?\s+(?:/[ck]\s+)?"
        r"|\b(?:run|execute)\s+(?:the\s+|this\s+|a\s+)?"
        r"(?:command|cmd|script|program|tests?|build|task|file)\b"
        r"|\b(?:run|execute)\s+(?:`[^`]+`|'[^']+'|\"[^\"]+\")"
        r"|\brun\s+(?:whoami|dir|ipconfig|npm|python|git|cargo|pytest|powershell)\b"
        r"|^\s*(?:(?:please|can you|could you|would you)\s+)?(?:run|execute)\s+"
        r"(?!(?:me|the|a|an|through|for|by|with|over|on|this|that|it|away|late|fast)\b)"
        r"[a-z_][\w.-]*(?:\s+.*)?$",
        re.I,
    )
    _search = re.compile(
        r"\b(?:find|search(?:\s+for)?|locate|look\s+for|look\s+up|"
        r"track\s+down|where\s+(?:is|are|can\s+i\s+find))\b",
        re.I,
    )
    _powershell_request = re.compile(
        r"\b(?:use|run|execute)\s+(?:windows\s+)?(?:powershell|pwsh)\b"
        r"|^\s*(?:powershell|pwsh)\s*[:>]\s*\S"
        r"|^\s*(?:powershell|pwsh)\s+(?:-command\s+)?"
        r"|^\s*(?:Get|New|Copy|Move|Rename|Set|Start|Stop|Test|Select|Where|Measure|Format|Out|Write|Read|Import|Export)-[A-Za-z][\w-]*\b",
        re.I,
    )
    _delete_file = re.compile(r"\b(?:delete|remove|trash)\b", re.I)
    _create_file = re.compile(
        r"^\s*(?:(?:please|kindly|i need you to|i need to|i want you to|i want to|"
        r"can you|could you|would you)\s+)?(?:create|make|write|save|generate)\b"
        r".{0,80}\b(?:new\s+)?file\b"
        r"|^\s*(?:(?:please|kindly|can you|could you|would you|i want you to|"
        r"i need you to)\s+)?(?:create|make|write|save|generate)\s+(?:a\s+)?(?:new\s+)?"
        r"[\w ._-]+\.[a-z0-9]{1,10}\b",
        re.I,
    )
    _new_file = re.compile(
        r"^\s*(?:(?:please|kindly|can you|could you|would you)\s+)?"
        r"(?:start|begin)\s+(?:a\s+)?new\s+file\b|"
        r"^\s*(?:(?:please|kindly|can you|could you|would you)\s+)?"
        r"new\s+(?:text\s+)?file\b",
        re.I,
    )
    _named_create_target = re.compile(
        r"\b(?:named|called)\s+[\"'`]?"
        r"([^\"'`<>|?*\r\n]+?\.[a-z0-9]{1,10})"
        r"(?=[\"'`]?(?:$|\s|[,!?]))",
        re.I,
    )
    _bare_create_target = re.compile(
        r"^\s*(?:(?:please|kindly|can you|could you|would you|"
        r"i want you to|i need you to)\s+)?(?:create|make|write|save|generate)\s+"
        r"(?:(?:a|an|new|text|file)\s+){0,4}[\"'`]?"
        r"([^\"'`<>|?*\r\n]+?\.[a-z0-9]{1,10})"
        r"(?=[\"'`]?(?:$|\s|[,!?]))",
        re.I,
    )
    _new_file_target = re.compile(
        r"^\s*(?:(?:please|kindly|can you|could you|would you)\s+)?"
        r"(?:new\s+(?:text\s+)?file|(?:start|begin)\s+(?:a\s+)?new\s+file)\s+"
        r"(?:(?:named|called)\s+)?[\"'`]?([^\"'`<>|?*\r\n]+?\.[a-z0-9]{1,10})"
        r"(?=[\"'`]?(?:$|\s|[,!?]))",
        re.I,
    )
    _create_content = re.compile(
        r"\b(?:with\s+(?:the\s+)?(?:content|contents|text|following)"
        r"|containing|include(?:\s+the\s+following)?(?:\s+text|\s+content)?)"
        r"\s*[:=]?\s*(.*)$",
        re.I | re.S,
    )
    _generated_file_content = re.compile(
        r"\b(?:about|including|include|containing|draft|outline|summary|"
        r"with\s+(?:a\s+|an\s+|the\s+|some\s+)?"
        r"(?:short\s+|brief\s+)?(?:plan|report|summary|outline|draft|content|text|notes?))\b",
        re.I,
    )
    _action_question = re.compile(
        r"^\s*(?:how|why|when|what|should\s+i|can\s+i)\b.*"
        r"\b(?:delete|remove|trash|create|make|write|save|open|browse|find|search|"
        r"locate|list|read|summarize|show|run|execute|command|cmd|powershell|pwsh)\b",
        re.I,
    )
    _open_file = re.compile(
        r"\b(?:open|open\s+up|view|show|launch|pull\s+up|bring\s+up)\b.{0,48}"
        r"\b(?:file|document|pdf|docx|notes?)\b"
        r"|\b(?:open|open\s+up|view|launch|pull\s+up|bring\s+up)\s+"
        r"[\"']?[\w./\\:-]+\.(?:txt|md|pdf|docx)\b",
        re.I,
    )
    _open_folder = re.compile(
        r"\b(?:open|show|view|browse|go\s+to|navigate\s+to|take\s+me\s+to|bring\s+up)\b"
        r".{0,100}\b(?:folder|directory)\b",
        re.I,
    )
    _open_named_root = re.compile(
        r"^\s*(?:(?:please|kindly|can you|could you|would you|"
        r"i want you to|i need you to)\s+)?(?:open|show|view|browse|go\s+to|navigate\s+to|"
        r"take\s+me\s+to|bring\s+up)\s+"
        r"(?:(?:me|the|my)\s+)*(?P<root>documents|downloads|desktop|onedrive)"
        r"(?:\s+(?:folder|directory))?\s*[?.!]*$",
        re.I,
    )
    _read_file = re.compile(
        r"\b(?:read|summari[sz]e|inspect|extract)\b|"
        r"\bwhat(?:'s| is)\s+in\b.{0,100}\.(?:txt|md|pdf|docx)\b|"
        r"\bwhat\s+does\s+.{1,100}\.(?:txt|md|pdf|docx)\s+(?:say|contain|include)\b",
        re.I,
    )
    _list_folder = re.compile(
        r"\blist(?:\s+(?:files|contents))?(?:\s+(?:in|of))?\b"
        r"|\bwhat(?:'s| is)\s+(?:in|inside)\b|\bwhat\s+files\s+(?:are\s+)?(?:in|inside)\b"
        r"|\bcontents\s+of\b"
        r"|\b(?:show|display|browse)\s+(?:me\s+)?(?:the\s+)?"
        r"(?:files|contents|items)\s+(?:in|of)\b",
        re.I,
    )
    _open_target = re.compile(
        r"(?:\b(?:(?:open|open\s+up|view|show|launch|pull\s+up|bring\s+up|read|summari[sz]e|inspect|extract)"
        r"\s+(?:me\s+)?(?:the\s+)?(?:(?:attached|this|that|my)\s+)?(?:file\s+)?|"
        r"what(?:'s| is)\s+in\s+)|\bwhat\s+does\s+(?:the\s+)?(?:file\s+)?"
        r")"
        r"[\"']?([^\"'<>|?*\r\n]+?\.[a-z0-9]{1,10})[\"']?(?=$|\s|[,!?])",
        re.I,
    )
    _list_target = re.compile(
        r"\b(?:list(?:\s+(?:files|contents))?(?:\s+(?:in|of))?|"
        r"what(?:'s| is)\s+(?:in|inside)\b|what\s+files\s+(?:are\s+)?(?:in|inside)\b|contents\s+of|"
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
        prompt = self.normalize_action_typos(prompt)
        match = self._confirm_delete.search(prompt.strip())
        return match.group(1).upper() if match else None

    def cancellation_token(self, prompt: str) -> str | None:
        prompt = self.normalize_action_typos(prompt)
        match = self._cancel_delete.search(prompt.strip())
        return match.group(1).upper() if match else None

    def delete_target(self, prompt: str) -> str | None:
        prompt = self.normalize_action_typos(prompt)
        match = self._delete_target.search(prompt.strip())
        return match.group(1).strip().strip("`").rstrip(".,;") if match else None

    def open_target(self, prompt: str) -> str | None:
        """Extract an explicitly named file for safe, model-free read or open."""
        prompt = self.normalize_action_typos(prompt)
        match = self._open_target.search(prompt.strip())
        if match is None:
            return None
        return match.group(1).strip().rstrip(".,;") or None

    def list_target(self, prompt: str) -> str | None:
        """Extract an optional folder name from a natural-language listing request."""
        prompt = self.normalize_action_typos(prompt)
        match = self._list_target.search(prompt.strip())
        if match is None:
            return None
        target = match.group("path").strip().strip('"').strip("'").rstrip(".,;?!")
        target = re.sub(r"^(?:files|contents)\s+(?:in|of)\s+", "", target, flags=re.I)
        target = re.sub(r"\s+(?:folder|directory)$", "", target, flags=re.I)
        return target or None

    def folder_target(self, prompt: str) -> str | None:
        """Extract an explicit folder name/path from a direct open-folder request."""
        prompt = self.normalize_action_typos(prompt)
        root = self._open_named_root.search(prompt.strip())
        if root is not None:
            return root.group("root")
        inside = re.search(
            r"\b(?:open|show|view|browse|go\s+to|navigate\s+to|take\s+me\s+to|bring\s+up)\s+"
            r"(?:(?:me|the|my)\s+)*(?P<path>.+?)\s+(?:folder|directory)\s+"
            r"(?:inside|in)\s+(?:my\s+)?(?:onedrive|documents|downloads|desktop)\s*[?.!]*$",
            prompt.strip(), re.I,
        )
        if inside is not None:
            target = inside.group("path").strip().strip('"').strip("'")
            target = re.sub(r"^(?:the|my)\s+", "", target, flags=re.I)
            return target or None
        match = re.search(
            r"\b(?:open|show|view|browse|go\s+to|navigate\s+to|take\s+me\s+to|bring\s+up)\s+"
            r"(?:(?:me|the|my)\s+)*(?P<path>.+?)\s+(?:folder|directory)\s*[?.!]*$",
            prompt.strip(), re.I,
        )
        if match is None:
            return None
        target = match.group("path").strip().strip('"').strip("'")
        target = re.sub(r"^(?:the|my)\s+", "", target, flags=re.I)
        return target or None

    def create_target(self, prompt: str) -> str | None:
        """Extract one explicit new-file path from a natural-language request."""
        text = self.normalize_action_typos(prompt).strip()
        match = (
            self._named_create_target.search(text)
            or self._bare_create_target.search(text)
            or self._new_file_target.search(text)
        )
        if match is None:
            return None
        target = match.group(1).strip().strip("`\"").rstrip(".,;!? ")
        return target or None

    def create_content(self, prompt: str) -> str | None:
        """Return content the user explicitly supplied, without asking a model to invent it."""
        match = self._create_content.search(self.normalize_action_typos(prompt).strip())
        if match is None:
            return None
        content = match.group(1).lstrip(" \t\r\n")
        if len(content) >= 2 and content[0] == content[-1] and content[0] in "\"'`":
            content = content[1:-1]
        return content

    def create_needs_generated_content(self, prompt: str) -> bool:
        """Keep requests asking FileBot to author text on the model-backed create tool."""
        text = self.normalize_action_typos(prompt).strip()
        target = (
            self._named_create_target.search(text)
            or self._bare_create_target.search(text)
            or self._new_file_target.search(text)
        )
        suffix = text[target.end():] if target is not None else text
        return bool(self._generated_file_content.search(suffix))

    def is_summary_request(self, prompt: str) -> bool:
        """Keep summary requests model-backed even when the named file is local."""
        return bool(self._summary_request.search(self.normalize_action_typos(prompt)))

    @staticmethod
    def _edit_distance(left: str, right: str) -> int:
        """Small Damerau-Levenshtein distance, including one adjacent-key transposition."""
        rows, columns = len(left), len(right)
        table = [[0] * (columns + 1) for _ in range(rows + 1)]
        for row in range(rows + 1):
            table[row][0] = row
        for column in range(columns + 1):
            table[0][column] = column
        for row in range(1, rows + 1):
            for column in range(1, columns + 1):
                cost = 0 if left[row - 1] == right[column - 1] else 1
                table[row][column] = min(
                    table[row - 1][column] + 1,
                    table[row][column - 1] + 1,
                    table[row - 1][column - 1] + cost,
                )
                if (row > 1 and column > 1
                        and left[row - 1] == right[column - 2]
                        and left[row - 2] == right[column - 1]):
                    table[row][column] = min(table[row][column], table[row - 2][column - 2] + 1)
        return table[rows][columns]

    @classmethod
    def normalize_action_typos(cls, prompt: str) -> str:
        """Correct near-miss action words in the command prefix, never file extensions."""
        spans = list(re.finditer(r"[A-Za-z][A-Za-z-]*", prompt))
        replacements: list[tuple[int, int, str]] = []
        if not spans or spans[0].group(0).casefold() in {"how", "why", "when", "what"}:
            return prompt

        polite = {"please", "kindly", "can", "could", "would", "you", "i", "want", "need", "to", "the", "windows"}
        first = 0
        while first < len(spans) and first < 5 and spans[first].group(0).casefold() in polite:
            first += 1

        def closest(word: str, candidates: tuple[str, ...]) -> str | None:
            if len(word) < 3:
                return None
            possible = [candidate for candidate in candidates if abs(len(candidate) - len(word)) <= 2]
            if not possible:
                return None
            best = min(
                possible,
                key=lambda candidate: (
                    cls._edit_distance(word, candidate),
                    -SequenceMatcher(None, word, candidate).ratio(),
                ),
            )
            distance = cls._edit_distance(word, best)
            limit = 2 if len(best) >= 6 and len(word) >= 4 else 1
            if distance <= limit and SequenceMatcher(None, word, best).ratio() >= 0.64:
                return best
            return None

        if first < len(spans):
            span = spans[first]
            corrected = closest(span.group(0).casefold(), (
                "open", "show", "view", "browse", "find", "search", "locate", "read",
                "summarize", "list", "delete", "remove", "trash", "create", "make",
                "write", "save", "generate", "run", "execute", "launch", "start",
                "take", "bring", "go", "navigate", "dir", "cd", "tree", "type",
                "where", "whoami", "echo", "help", "ipconfig", "tasklist", "cmd",
                "command", "powershell", "pwsh",
            ))
            if corrected:
                replacements.append((span.start(), span.end(), corrected))

        for index in range(first + 1, min(len(spans), 8)):
            span = spans[index]
            word = span.group(0).casefold()
            previous = spans[index - 1].group(0).casefold()
            tail = prompt[span.end():].lstrip("\"'` ")
            if word in cls._action_words or (tail.startswith(".") and re.match(r"\.\w+", tail)):
                continue
            candidates: tuple[str, ...] = ()
            if previous in {"cmd", "command"} or closest(previous, ("cmd", "command")):
                candidates = ("prompt",)
            elif previous == "file":
                candidates = ("named", "called")
            elif previous in {"open", "show", "view", "browse", "take", "bring"}:
                candidates = ("folder", "directory", "prompt", "cmd", "command", "terminal")
            elif previous in {"use", "run", "execute"}:
                candidates = ("powershell", "pwsh", "cmd")
            else:
                candidates = ("folder", "directory")
            corrected = closest(word, candidates)
            if corrected:
                replacements.append((span.start(), span.end(), corrected))
        for start, end, replacement in reversed(replacements):
            prompt = prompt[:start] + replacement + prompt[end:]
        return prompt

    def command_text(self, prompt: str, *, shell: str = "cmd") -> str | None:
        """Extract an explicitly prefixed shell command without changing its bytes."""
        text = prompt.strip()
        prefix = self._cmd_prefix if shell == "cmd" else self._powershell_prefix
        match = prefix.search(text)
        if match:
            command = match.group("command").strip().strip("`\"'").rstrip("?!")
            if shell == "cmd":
                normalized = self.normalize_action_typos(command)
                if self._bare_cmd.search(normalized):
                    command = normalized
            return command or None
        if shell == "cmd":
            match = re.match(
                r"^\s*cmd(?:\.exe)?\s+(?:/[ck]\s+)?(?P<command>.+)$", text, re.I,
            )
            if match:
                command = match.group("command").strip().strip("`\"'").rstrip("?!")
                normalized = self.normalize_action_typos(command)
                if self._bare_cmd.search(normalized):
                    command = normalized
                return command or None
            match = re.match(
                r"^\s*(?:(?:please|kindly|can you|could you|would you)\s+)?"
                r"(?:run|execute)\s+(?P<command>.+)$", text, re.I,
            )
            if match:
                command = match.group("command").strip()
                if not re.match(r"^(?:me|the|a|an|through|for|by|with|over|on|this|that|it|away|late|fast)\b", command, re.I):
                    return command.strip("`\"'").rstrip("?!") or None
            normalized = self.normalize_action_typos(text)
            if self._bare_cmd.search(normalized):
                return normalized.rstrip("?!")
        return None

    def route(self, prompt: str) -> str:
        """Route prompt intent; CMD and delete remain behind explicit confirmations."""
        prompt = self.normalize_action_typos(prompt)
        if self._confirm_delete.search(prompt) or self._cancel_delete.search(prompt):
            return "confirm_delete" if self._confirm_delete.search(prompt) else "cancel_delete"
        if self._cmd_prompt_open.search(prompt) or self._cmd_help.search(prompt):
            return "cmd_help"
        if self._action_question.search(prompt):
            return "chat"
        if self._powershell_request.search(prompt):
            return "powershell"
        if self._cmd.search(prompt) or self._bare_cmd.search(prompt):
            return "cmd"
        if self._delete_file.search(prompt):
            return "delete"
        if self._create_file.search(prompt) or self._new_file.search(prompt):
            return "create"
        if self._open_folder.search(prompt) or self._open_named_root.search(prompt):
            return "open_folder"
        if self._search.search(prompt) and not self._action_question.search(prompt):
            return "search"
        if self._read_file.search(prompt):
            return "read"
        if self._open_file.search(prompt):
            return "open"
        if self._list_folder.search(prompt):
            return "list"
        return "chat"
