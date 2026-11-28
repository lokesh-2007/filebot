"""Classify single CMD/PowerShell proposals from config/cmd_policy.yaml."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

MAX_COMMAND_LENGTH = 4096


class PolicyEngine:
    """Classify full commands; blocked patterns win and every runnable command needs approval."""
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
        cmd_default = data.get("cmd_default", "blocked")
        if cmd_default not in {"blocked", "gated"}:
            raise RuntimeError("Command policy 'cmd_default' must be 'blocked' or 'gated'")
        self.cmd_default = cmd_default
        self.patterns = patterns
        powershell = data.get("powershell", {})
        if not isinstance(powershell, dict):
            raise RuntimeError("Command policy 'powershell' section must be a mapping")
        self.powershell_patterns: dict[str, list[re.Pattern[str]]] = {}
        for key in ("blocked", "read_only", "gated"):
            entries = powershell.get(key, [])
            if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
                raise RuntimeError(f"PowerShell policy {key!r} entries must be a list of strings")
            try:
                self.powershell_patterns[key] = [re.compile(item, re.IGNORECASE) for item in entries]
            except re.error as exc:
                raise RuntimeError(f"PowerShell policy contains an invalid {key!r} expression: {exc}") from exc

    def classify(self, command: str, *, shell: str = "cmd") -> str:
        """Return blocked, read_only, or gated for a non-empty command."""
        if shell not in {"cmd", "powershell"} or len(command) > MAX_COMMAND_LENGTH:
            return "blocked"
        value = command.strip()
        # Windows CMD treats CR/LF as command boundaries. Reject all ASCII
        # control characters before evaluating allowlists so a multiline string
        # cannot smuggle a second command through whitespace-tolerant patterns.
        if not value or any(ord(character) < 0x20 or ord(character) == 0x7F
                            for character in value):
            return "blocked"
        patterns = self.patterns if shell == "cmd" else self.powershell_patterns
        for pattern in patterns["blocked"]:
            if pattern.search(value):
                return "blocked"
        for level in ("read_only", "gated"):
            if any(pattern.fullmatch(value) for pattern in patterns[level]):
                return level
        return self.cmd_default if shell == "cmd" else self.default
