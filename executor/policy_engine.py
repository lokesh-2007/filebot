"""Default-deny command classification from config/cmd_policy.yaml."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

MAX_COMMAND_LENGTH = 4096


class PolicyEngine:
    """Classify full command strings; explicit blocks always take precedence."""
    def __init__(self, policy_path: str | Path) -> None:
        import yaml
        try:
            data = yaml.safe_load(Path(policy_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Cannot load command policy: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Command policy must be a YAML mapping")
        if data.get("default", "blocked") != "blocked":
            raise RuntimeError("Command policy default must remain 'blocked'")
        patterns: dict[str, list[re.Pattern[str]]] = {}
        for key in ("blocked", "read_only", "gated"):
            entries = data.get(key, [])
            if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
                raise RuntimeError(f"Command policy {key!r} entries must be a list of strings")
            try:
                patterns[key] = [re.compile(item, re.IGNORECASE) for item in entries]
            except re.error as exc:
                raise RuntimeError(f"Command policy contains an invalid {key!r} regular expression: {exc}") from exc
        self.default = "blocked"
        self.patterns = patterns

    def classify(self, command: str) -> str:
        """Return blocked, read_only, or gated for a non-empty command."""
        if len(command) > MAX_COMMAND_LENGTH:
            return "blocked"
        value = command.strip()
        # Windows CMD treats CR/LF as command boundaries. Reject all ASCII
        # control characters before evaluating allowlists so a multiline string
        # cannot smuggle a second command through whitespace-tolerant patterns.
        if not value or any(ord(character) < 0x20 or ord(character) == 0x7F
                            for character in value):
            return "blocked"
        for pattern in self.patterns["blocked"]:
            if pattern.search(value):
                return "blocked"
        for level in ("read_only", "gated"):
            if any(pattern.fullmatch(value) for pattern in self.patterns[level]):
                return level
        return self.default
