"""Default-deny command classification from config/cmd_policy.yaml."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class PolicyEngine:
    """Classify full command strings; explicit blocks always take precedence."""
    def __init__(self, policy_path: str | Path) -> None:
        import yaml
        try:
            data = yaml.safe_load(Path(policy_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Cannot load command policy: {exc}") from exc
        self.default = str(data.get("default", "blocked"))
        self.patterns = {key: [re.compile(str(p), re.IGNORECASE) for p in data.get(key, [])]
                         for key in ("blocked", "read_only", "gated")}

    def classify(self, command: str) -> str:
        """Return blocked, read_only, or gated for a non-empty command."""
        value = command.strip()
        if not value or "\x00" in value:
            return "blocked"
        for pattern in self.patterns["blocked"]:
            if pattern.search(value):
                return "blocked"
        for level in ("read_only", "gated"):
            if any(pattern.fullmatch(value) for pattern in self.patterns[level]):
                return level
        return self.default
