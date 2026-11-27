"""Command proposal tool: classify and audit, never execute."""
from __future__ import annotations

from pathlib import Path

from executor.policy_engine import PolicyEngine
from storage.db import Database


class ProposeCmdTool:
    """Stage a command for visible user review; blocked commands stay blocked."""
    name = "propose_cmd"
    description = "Propose a Windows CMD command for policy review and explicit approval."

    def __init__(self, policy: PolicyEngine, db: Database) -> None:
        self.policy, self.db = policy, db

    def propose(self, command: str, reason: str, cwd: str | None = None) -> dict[str, str]:
        level = self.policy.classify(command)
        if level == "blocked":
            proposal_id = self.db.stage_command(command, cwd or str(Path.home()), reason, level, "blocked")
            return {"proposal_id": proposal_id, "status": "blocked", "risk_level": level}
        if level == "read_only":
            # Read-only commands are still visible and auditable; approval is explicit
            # for every process invocation in the desktop UI.
            proposal_id = self.db.stage_command(command, cwd or str(Path.home()), reason, level, "pending")
            return {"proposal_id": proposal_id, "status": "pending", "risk_level": level}
        proposal_id = self.db.stage_command(command, cwd or str(Path.home()), reason, level, "pending")
        return {"proposal_id": proposal_id, "status": "pending", "risk_level": level}
