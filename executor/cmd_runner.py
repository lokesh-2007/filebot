
"""The only module allowed to start a Windows command, and only after approval."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from executor.policy_engine import PolicyEngine
from storage.db import Database


class CommandRunner:
    """Execute one explicitly approved audit row with bounded runtime/output."""
    def __init__(self, db: Database, timeout: int = 30,
                 policy_path: str | Path | None = None) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.db, self.timeout = db, timeout
        self.policy_path = Path(policy_path) if policy_path else Path(__file__).resolve().parents[1] / "config" / "cmd_policy.yaml"

    def execute(self, proposal_id: str) -> tuple[int, str, str]:
        """Run a staged row only if its database status is exactly approved."""
        row = self.db.claim_approved_command(proposal_id)
        if row is None:
            raise PermissionError("Command must have an explicit approved audit record")
        try:
            current_risk = PolicyEngine(self.policy_path).classify(str(row["command"]))
        except Exception as exc:
            message = f"Could not verify the current command policy: {exc}"
            self.db.finish_command(proposal_id, 126, "", message)
            return 126, "", message[-4000:]
        if current_risk == "blocked" or current_risk != row["risk_level"]:
            message = "Command is no longer allowed by the current policy"
            self.db.finish_command(proposal_id, 126, "", message)
            return 126, "", message
        system_root = os.environ.get("SystemRoot")
        system32 = Path(system_root) / "System32" if system_root else None
        if system32 is None or not system32.is_dir() or not (system32 / "cmd.exe").is_file():
            message = "Windows System32 command processor is unavailable"
            self.db.finish_command(proposal_id, 126, "", message)
            return 126, "", message

        command = str(row["command"])
        external_read_only = bool(re.match(
            r"^\s*(?:where|whoami|systeminfo|ipconfig|tasklist)(?:\s|$)", command, re.IGNORECASE
        ))
        # Prevent a user folder or a modified PATH from shadowing approved
        # Windows utilities. These built-ins do not need the proposed cwd.
        cwd = system32 if external_read_only else (Path(row["cwd"]).expanduser() if row["cwd"] else Path.home())
        if not cwd.is_dir():
            message = "Command working directory is not an existing directory"
            self.db.finish_command(proposal_id, 126, "", message)
            return 126, "", message
        try:
            completed = subprocess.run([str(system32 / "cmd.exe"), "/d", "/s", "/c", command],
                cwd=str(cwd), capture_output=True, text=True, errors="replace", timeout=self.timeout,
                check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = f"Command timed out after {self.timeout}s"
            code = 124
        except OSError as exc:
            stdout, stderr, code = "", str(exc), 127
        self.db.finish_command(proposal_id, code, stdout, stderr)
        return code, stdout[-4000:], stderr[-4000:]

