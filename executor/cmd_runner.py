"""The only module allowed to start a Windows command, and only after approval."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from storage.db import Database


class CommandRunner:
    """Execute one explicitly approved audit row with bounded runtime/output."""
    def __init__(self, db: Database, timeout: int = 30) -> None:
        self.db, self.timeout = db, timeout

    def execute(self, proposal_id: str) -> tuple[int, str, str]:
        """Run a staged row only if its database status is exactly approved."""
        row = self.db.command_record(proposal_id)
        if row is None or row["status"] != "approved":
            raise PermissionError("Command must have an explicit approved audit record")
        cwd = Path(row["cwd"]).expanduser() if row["cwd"] else Path.home()
        if not cwd.is_dir():
            raise ValueError("Command working directory is not an existing directory")
        try:
            completed = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", row["command"]],
                cwd=str(cwd), capture_output=True, text=True, errors="replace", timeout=self.timeout,
                check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = f"Command timed out after {self.timeout}s"
            code = 124
        self.db.finish_command(proposal_id, code, stdout, stderr)
        return code, stdout[-4000:], stderr[-4000:]
