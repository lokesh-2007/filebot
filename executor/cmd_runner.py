"""The only module allowed to start a Windows command, and only after approval."""
from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path

from executor.policy_engine import PolicyEngine
from storage.db import Database

_OUTPUT_TAIL_BYTES = 64 * 1024


def _read_output_tail(stream: object, limit: int = _OUTPUT_TAIL_BYTES) -> bytes:
    """Drain a subprocess pipe while retaining only its last ``limit`` bytes."""
    if limit < 1:
        raise ValueError("output tail limit must be positive")
    tail = bytearray()
    while True:
        chunk = stream.read(4096)  # type: ignore[attr-defined]
        if not chunk:
            return bytes(tail)
        tail.extend(chunk)
        if len(tail) > limit:
            del tail[:len(tail) - limit]


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
        readers: list[threading.Thread] = []
        output: dict[str, bytes] = {}
        try:
            process = subprocess.Popen(
                [str(system32 / "cmd.exe"), "/d", "/s", "/c", command],
                cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert process.stdout is not None and process.stderr is not None
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                reader = threading.Thread(
                    target=lambda key=name, pipe=stream: output.__setitem__(key, _read_output_tail(pipe)),
                    name=f"FileBotCMD-{name}", daemon=True,
                )
                readers.append(reader)
                reader.start()
            try:
                code = process.wait(timeout=self.timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                code = 124
                timed_out = True
            for reader in readers:
                reader.join(timeout=2)
            for stream in (process.stdout, process.stderr):
                stream.close()
            stdout = output.get("stdout", b"").decode("utf-8", errors="replace")
            stderr = output.get("stderr", b"").decode("utf-8", errors="replace")
            if timed_out:
                stderr = f"Command timed out after {self.timeout}s" + (f"\n{stderr}" if stderr else "")
        except OSError as exc:
            stdout, stderr, code = "", str(exc), 127
        self.db.finish_command(proposal_id, code, stdout, stderr)
        return code, stdout[-4000:], stderr[-4000:]
